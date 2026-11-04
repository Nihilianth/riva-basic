"""Manual-turn voice baseline: native Gradio ASR → short text reply → speech."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from riva.audio.recordings import RecordingStore, SessionIdentity
from riva.audio.replies import ReplyConfig, ReplyManager
from riva.audio.transcription import AsrConfig
from riva.event_log import Event, EventName, write_event
from riva.experiments.asr_stream import AsrSessions, build_vad_app
from riva.experiments.audio_capture import prepare_environment

if TYPE_CHECKING:
    import gradio as gr
    from gradio.events import Dependency

STATUS_LABELS = {
    "generating": "läuft",
    "completed": "abgeschlossen",
    "failed": "fehlgeschlagen",
    "cancelled": "abgebrochen",
}


def _milliseconds(metrics: dict[str, object], key: str) -> float | None:
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def format_metrics(metrics: dict[str, object]) -> str:
    """Human-readable latency panel; raw values remain in the JSON component."""
    status = metrics.get("status")
    status_label = (
        STATUS_LABELS.get(status, "unbekannt") if isinstance(status, str) else "unbekannt"
    )
    lines = [f"**Status:** {status_label}"]
    error = metrics.get("error_code")
    if isinstance(error, str) and error:
        lines.append(f"**Fehlercode:** `{error}`")
    text = _milliseconds(metrics, "first_text_ms")
    audio = _milliseconds(metrics, "first_server_audio_ms")
    playback = _milliseconds(metrics, "first_browser_play_event_ms")
    stopped = _milliseconds(metrics, "stopped_ms")
    playback_dur = _milliseconds(metrics, "playback_duration_ms")
    lines.append(f"- Erster Text: {text / 1000:.2f} s" if text is not None else "- Erster Text: –")
    lines.append(
        f"- Erstes Server-Audio: {audio / 1000:.2f} s"
        if audio is not None
        else "- Erstes Server-Audio: –"
    )
    if text is not None and audio is not None:
        lines.append(f"- Text bis Audio: {(audio - text) / 1000:.2f} s")
    lines.append(
        f"- Wiedergabe im Browser: {playback / 1000:.2f} s"
        if playback is not None
        else "- Wiedergabe im Browser: –"
    )
    if playback_dur is not None:
        lines.append(f"- Gehört vor Abbruch: {playback_dur / 1000:.2f} s")
    elif stopped is not None and status == "cancelled":
        lines.append(f"- Abbruch nach: {stopped / 1000:.2f} s")
    produced = _milliseconds(metrics, "audio_seconds_produced")
    if produced is not None:
        lines.append(f"- Erzeugte Audiodauer: {produced:.2f} s")
    lines.append("")
    lines.append(
        "*Serverwerte ab Antwortstart, Browserwert ab Klick. "
        "Wiedergabe ist ein Proxy, keine gemessene Hörbarkeit.*"
    )
    return "\n".join(lines)


def add_reply_controls(
    sessions: AsrSessions,
    replies: ReplyManager,
    identity: gr.State,
    finish_event: Dependency,
) -> None:
    import gradio as gr

    def enable(identity: SessionIdentity) -> gr.Button:
        result, _ = sessions.snapshot(identity)
        return gr.Button(
            interactive=result.status == "completed" and bool(result.text and result.text.strip())
        )

    def reply(
        identity: SessionIdentity, client_started_ms: float
    ) -> Iterator[tuple[Any, Any, Any, Any, Any, Any]]:
        try:
            turn = sessions.current(identity)
            if turn is None:
                raise ValueError("No active turn")
            result = turn.live.snapshot()
            if (
                not turn.persisted
                or turn.saved is None
                or result.status != "completed"
                or not result.text
            ):
                raise ValueError("No saved final transcript")
            run, created = replies.start(identity, turn.saved, result.text, client_started_ms)
        except (ValueError, KeyError, OSError):
            raise gr.Error(
                "Bitte zuerst eine gültige Transkription abschließen und speichern."
            ) from None
        if not created:
            snapshot = run.snapshot()
            yield (
                run.text,
                gr.skip(),
                snapshot,
                format_metrics(snapshot),
                gr.Button(interactive=True),
                gr.Button(interactive=False),
            )
            return
        write_event(
            Event(
                EventName.RESPONSE_STARTED,
                identity.session_id,
                turn.saved.metadata.turn_id,
                run.generation_id,
            ),
            sys.stdout,
        )
        initial = run.snapshot()
        yield (
            "",
            None,
            initial,
            format_metrics(initial),
            gr.Button(interactive=False),
            gr.Button(interactive=True),
        )
        for text, wav, metrics in run.updates():
            yield (
                text,
                wav if wav is not None else gr.skip(),
                metrics,
                format_metrics(metrics),
                gr.Button(interactive=False),
                gr.Button(interactive=True),
            )
        name = (
            EventName.RESPONSE_COMPLETED
            if run.status == "completed"
            else (
                EventName.PLAYBACK_STOPPED
                if run.status == "cancelled"
                else EventName.RESPONSE_FAILED
            )
        )
        write_event(
            Event(
                name,
                identity.session_id,
                turn.saved.metadata.turn_id,
                run.generation_id,
                duration_ms=run.snapshot().get("playback_duration_ms"),  # type: ignore[arg-type]
            ),
            sys.stdout,
        )
        final_snapshot = run.snapshot()
        yield (
            run.text,
            gr.skip(),
            final_snapshot,
            format_metrics(final_snapshot),
            gr.Button(interactive=True),
            gr.Button(interactive=False),
        )

    def stop_reply(
        identity: SessionIdentity, client_now_ms: float
    ) -> tuple[Any, Any, Any, Any, Any, Any]:
        run = replies.stop(identity, client_now_ms)
        if run is not None:
            write_event(
                Event(
                    EventName.PLAYBACK_STOPPED,
                    identity.session_id,
                    run.saved.metadata.turn_id,
                    run.generation_id,
                    duration_ms=run.snapshot().get("playback_duration_ms"),  # type: ignore[arg-type]
                ),
                sys.stdout,
            )
            snapshot = run.snapshot()
            return (
                run.text,
                None,
                snapshot,
                format_metrics(snapshot),
                gr.Button(interactive=True),
                gr.Button(interactive=False),
            )
        return (
            gr.skip(),
            None,
            gr.skip(),
            gr.skip(),
            gr.Button(interactive=True),
            gr.Button(interactive=False),
        )

    def played(
        identity: SessionIdentity, _metrics: dict[str, Any], now_ms: float
    ) -> tuple[dict[str, Any], str]:
        run = replies.current(identity)
        write_event(
            Event(
                EventName.PLAYBACK_EVENT,
                identity.session_id,
                run.saved.metadata.turn_id if run is not None else None,
                run.generation_id if run is not None else None,
            ),
            sys.stdout,
        )
        if run is None:
            return cast("dict[str, Any]", gr.skip()), cast("str", gr.skip())
        try:
            recorded = run.record_playback(now_ms)
        except OSError:
            metrics = {**run.snapshot(), "metrics_storage_error": True}
            return metrics, format_metrics(metrics)
        snapshot = run.snapshot()
        if not recorded:
            return snapshot, format_metrics(snapshot)
        write_event(
            Event(
                EventName.PLAYBACK_STARTED,
                identity.session_id,
                run.saved.metadata.turn_id,
                run.generation_id,
            ),
            sys.stdout,
        )
        return snapshot, format_metrics(snapshot)

    gr.Markdown(
        "## Kurze KI-Antwort\n"
        "Nach dem Speichern die endgültige Transkription prüfen, dann **Antwort erzeugen**. "
        "Text und synthetische Stimme werden von OpenAI erzeugt (zusätzliche API-Kosten). "
        "**Die Stimme ist KI-generiert, kein Mensch.** "
        "Über **Wiedergabe abbrechen** kann die Antwort jederzeit gestoppt werden.\n\n"
        "Noch keine Gesprächserinnerung, kein LangGraph, keine automatische Unterbrechung. "
        "Speech-API-Modell laut OpenAI nur bis 06.01.2027 verfügbar; Migration ist separat erfasst."
    )
    with gr.Row():
        button = gr.Button("Antwort erzeugen", interactive=False, variant="primary")
        stop_button = gr.Button("Wiedergabe abbrechen", interactive=False, variant="stop")
    clock = gr.Number(value=0, visible=False)
    text = gr.Textbox(label="KI-Antwort (streamend)", interactive=False)
    speech = gr.Audio(
        label="KI-Stimme", streaming=True, autoplay=True, interactive=False, buttons=[]
    )
    metrics = gr.JSON(label="Antwortstatus und Latenz (ms)")
    latency = gr.Markdown(value="Noch keine Antwort erzeugt.", label="Latenz (lesbar)")
    finish_event.success(enable, inputs=[identity], outputs=[button], api_visibility="private")
    reply_event = button.click(
        reply,
        inputs=[identity, clock],
        outputs=[text, speech, metrics, latency],
        js="(identity, _) => [identity, performance.now()]",
        concurrency_limit=1,
        api_visibility="private",
    )
    stop_button.click(
        stop_reply,
        inputs=[identity, clock],
        outputs=[text, speech, metrics, latency],
        js=r"""(identity, _) => {
  document.querySelectorAll('audio').forEach((audio) => {
    try {
      audio.pause();
      audio.currentTime = 0;
      audio.removeAttribute('src');
      audio.load();
    } catch (_) {}
  });
  window.rivaCurrentGeneration = null;
  window.rivaGenerationRecorded = true;
  return [identity, performance.now()];
}""",
        queue=False,
        api_visibility="private",
    )
    reply_event.then(
        lambda: (gr.Button(interactive=True), gr.Button(interactive=False)),
        inputs=[],
        outputs=[button, stop_button],
        queue=False,
        api_visibility="private",
    )
    speech.play(
        played,
        inputs=[identity, metrics, clock],
        outputs=[metrics, latency],
        js="(identity, metrics, _) => [identity, metrics, performance.now()]",
        queue=False,
        api_visibility="private",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--private-dir", type=Path, default=Path(os.environ.get("RIVA_PRIVATE_DIR", "../private"))
    )
    parser.add_argument("--port", type=int, default=7861)
    parser.add_argument("--llm-model", default=os.environ.get("RIVA_LLM_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--tts-model", default=os.environ.get("RIVA_TTS_MODEL", "gpt-4o-mini-tts"))
    parser.add_argument("--voice", default=os.environ.get("RIVA_TTS_VOICE", "coral"))
    args = parser.parse_args()
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        parser.error("OPENAI_API_KEY missing. Run: uv run --env-file .env riva-voice-test")
    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")
    root = prepare_environment(args.private_dir)
    sessions = AsrSessions(RecordingStore(root / "research-data" / "voice-test"), AsrConfig())
    replies = ReplyManager(
        ReplyConfig(llm_model=args.llm_model, tts_model=args.tts_model, voice=args.voice)
    )
    demo = build_vad_app(sessions, replies)
    playback_js = r"""
window.rivaPlaybackInstalled = window.rivaPlaybackInstalled || false;
if (!window.rivaPlaybackInstalled) {
  window.rivaPlaybackInstalled = true;
  window.rivaCurrentGeneration = null;
  window.rivaGenerationRecorded = false;
  document.addEventListener('click', (event) => {
    const button = event.target && event.target.closest ? event.target.closest('button') : null;
    if (button && button.textContent && button.textContent.includes('Antwort erzeugen')) {
      window.rivaGenerationRecorded = false;
      window.rivaCurrentGeneration = null;
    }
    if (button && button.textContent && button.textContent.includes('Wiedergabe abbrechen')) {
      window.rivaGenerationRecorded = true;
      window.rivaCurrentGeneration = null;
      document.querySelectorAll('audio').forEach((audio) => {
        try {
          audio.pause();
          audio.currentTime = 0;
          audio.removeAttribute('src');
          audio.load();
        } catch (_) {}
      });
    }
  }, true);
  setInterval(() => {
    if (window.rivaGenerationRecorded) return;
    const match = document.body.innerText.match(/"generation_id":\s*"([a-f0-9]+)"/);
    if (match) window.rivaCurrentGeneration = match[1];
  }, 250);
  const bindAudio = () => {
    document.querySelectorAll('audio').forEach((audio) => {
      if (audio.dataset.rivaPlayBound === '1') return;
      audio.dataset.rivaPlayBound = '1';
      audio.addEventListener('play', () => {
        if (window.rivaGenerationRecorded) return;
        const generationId = window.rivaCurrentGeneration;
        if (!generationId) {
          try {
            audio.pause();
          } catch (_) {}
          return;
        }
        window.rivaGenerationRecorded = true;
        fetch('/riva/playback', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({generation_id: generationId, now_ms: performance.now()})
        }).catch(() => {});
      });
    });
  };
  setInterval(bindAudio, 500);
}
"""
    import uvicorn
    from fastapi import FastAPI, Request
    from gradio.routes import mount_gradio_app

    demo.queue(max_size=64)
    api = FastAPI()

    @api.post("/riva/playback")
    async def record_browser_playback(request: Request) -> dict[str, bool]:
        try:
            data = await request.json()
        except Exception:
            return {"recorded": False}
        generation_id = data.get("generation_id") if isinstance(data, dict) else None
        now_ms = data.get("now_ms") if isinstance(data, dict) else None
        if not isinstance(generation_id, str):
            return {"recorded": False}
        try:
            now_value = float(now_ms)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return {"recorded": False}
        recorded = replies.record_browser_playback(generation_id, now_value)
        replies.note_playback_signal(generation_id)
        run = replies.find(generation_id)
        if run is not None:
            write_event(
                Event(
                    EventName.PLAYBACK_EVENT,
                    run.saved.metadata.session_id,
                    run.saved.metadata.turn_id,
                    generation_id,
                ),
                sys.stdout,
            )
            if recorded:
                write_event(
                    Event(
                        EventName.PLAYBACK_STARTED,
                        run.saved.metadata.session_id,
                        run.saved.metadata.turn_id,
                        generation_id,
                    ),
                    sys.stdout,
                )
            elif run.playback_signal_received:
                write_event(
                    Event(
                        EventName.PLAYBACK_FALLBACK,
                        run.saved.metadata.session_id,
                        run.saved.metadata.turn_id,
                        generation_id,
                    ),
                    sys.stdout,
                )
        return {"recorded": recorded}

    api = mount_gradio_app(
        api,
        demo,
        path="/",
        app_kwargs={},
        js=playback_js,
        max_file_size="30mb",
        show_error=False,
    )
    print(f"Voice test: http://localhost:{args.port}; OpenAI ASR, LLM and TTS enabled")
    print("Speech API models are deprecated; announced shutdown 2027-01-06 (riva-ddc).")
    print("Browser playback timing uses a JavaScript observer.")
    uvicorn.run(api, host="127.0.0.1", port=args.port, log_level="warning")
