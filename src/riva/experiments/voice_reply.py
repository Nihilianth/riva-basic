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
from riva.experiments.asr_stream import AsrSessions, asr_app_kwargs, build_asr_app
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
    ) -> Iterator[tuple[Any, Any, Any, str]]:
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
            yield run.text, gr.skip(), snapshot, format_metrics(snapshot)
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
        yield "", None, initial, format_metrics(initial)
        for text, wav, metrics in run.updates():
            yield (
                text,
                wav if wav is not None else gr.skip(),
                metrics,
                format_metrics(metrics),
            )
        name = (
            EventName.RESPONSE_COMPLETED if run.status == "completed" else EventName.RESPONSE_FAILED
        )
        write_event(
            Event(name, identity.session_id, turn.saved.metadata.turn_id, run.generation_id),
            sys.stdout,
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
        "Bitte erst nach Ende der Wiedergabe neu aufnehmen.\n\n"
        "Noch keine Gesprächserinnerung, kein LangGraph, keine Unterbrechung. "
        "Speech-API-Modell laut OpenAI nur bis 06.01.2027 verfügbar; Migration ist separat erfasst."
    )
    button = gr.Button("Antwort erzeugen", interactive=False, variant="primary")
    clock = gr.Number(value=0, visible=False)
    text = gr.Textbox(label="KI-Antwort (streamend)", interactive=False)
    speech = gr.Audio(
        label="KI-Stimme", streaming=True, autoplay=True, interactive=False, buttons=[]
    )
    metrics = gr.JSON(label="Antwortstatus und Latenz (ms)")
    latency = gr.Markdown(value="Noch keine Antwort erzeugt.", label="Latenz (lesbar)")
    finish_event.success(enable, inputs=[identity], outputs=[button], api_visibility="private")
    button.click(
        reply,
        inputs=[identity, clock],
        outputs=[text, speech, metrics, latency],
        js="(identity, _) => [identity, performance.now()]",
        concurrency_limit=1,
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
    app = build_asr_app(sessions, replies)
    print(f"Voice test: http://localhost:{args.port}; OpenAI ASR, LLM and TTS enabled")
    print("Speech API models are deprecated; announced shutdown 2027-01-06 (riva-ddc).")
    app.queue(max_size=64).launch(
        server_name="127.0.0.1",
        server_port=args.port,
        share=False,
        inbrowser=False,
        show_error=False,
        max_file_size="30mb",
        quiet=True,
        app_kwargs=asr_app_kwargs(app),
    )
