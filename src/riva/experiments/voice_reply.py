"""Manual-turn voice baseline: native Gradio ASR → short text reply → speech."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from riva.audio.recordings import RecordingStore, SessionIdentity
from riva.audio.replies import ReplyConfig, ReplyManager
from riva.audio.transcription import AsrConfig
from riva.event_log import Event, EventName, write_event
from riva.experiments.asr_stream import AsrSessions, build_vad_app
from riva.experiments.audio_capture import prepare_environment

if TYPE_CHECKING:
    import gradio as gr
    from gradio.events import Dependency

GENERATION_ID = re.compile(r"[a-f0-9]{32}")

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


def add_reply_audio_route(api: FastAPI, replies: ReplyManager) -> None:
    @api.get("/riva/reply_audio/{generation_id}")
    async def reply_audio(generation_id: str) -> StreamingResponse:
        """Stream a run's raw 24 kHz mono s16le PCM as it is produced."""
        run = replies.find(generation_id) if GENERATION_ID.fullmatch(generation_id) else None
        if run is None:
            raise HTTPException(status_code=404)
        deadline = run.started + run.config.max_seconds + 5

        async def chunks() -> AsyncIterator[bytes]:
            offset = 0
            while time.monotonic() < deadline:
                data, finished = run.read_audio(offset)
                if data:
                    offset += len(data)
                    yield data
                if finished:
                    return
                await asyncio.sleep(0.04)

        return StreamingResponse(
            chunks(),
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )


PLAYBACK_JS = r"""
window.rivaPlaybackInstalled = window.rivaPlaybackInstalled || false;
if (!window.rivaPlaybackInstalled) {
  window.rivaPlaybackInstalled = true;
  const reported = new Map();
  let reportCount = 0;
  // Allowlisted diagnostics only: an event kind plus a short identifier token.
  const report = (event, detail) => {
    const token = String(detail || '').replace(/[^A-Za-z0-9_.:-]/g, '').slice(0, 48);
    if (!token || reportCount >= 200) return;
    const key = event + '/' + token;
    const now = performance.now();
    if (reported.has(key) && now - reported.get(key) < 500) return;
    reported.set(key, now);
    reportCount += 1;
    fetch('/riva/browser_event', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({event: event, detail: token})
    }).catch(() => {});
  };
  window.rivaReport = report;
  // Read-only counters for tests/diagnostics: how often each player stage happened.
  const stats = window.rivaPlayerStats = {counts: {}, last: '', chunks: 0, ctx: 'none'};
  const note = (name) => {
    stats.counts[name] = (stats.counts[name] || 0) + 1;
    stats.last = name;
    if (window.rivaVadStage) window.rivaVadStage('audio ' + name);
    report('audio', name);
  };
  const userSpeaking = () => Boolean(window.rivaVad && window.rivaVad.speaking);

  // Reply playback: raw 24 kHz mono s16le PCM from /riva/reply_audio/<id>, played with Web
  // Audio. Gradio's HLS <audio> element is deliberately not used for VAD replies.
  const RATE = 24000;
  let ctx = null;
  let current = null;
  const ensureContext = () => {
    if (!ctx) {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      try { ctx = new Ctor({sampleRate: RATE}); } catch (_) { ctx = new Ctor(); }
      ctx.onstatechange = () => { stats.ctx = ctx.state; note('ctx_' + ctx.state); };
      note('ctx_' + ctx.state);
    }
    return ctx;
  };
  const unlock = () => {
    const c = ensureContext();
    if (c.state === 'suspended' && !(current && current.suspended)) c.resume().catch(() => {});
  };
  ['pointerdown', 'keydown', 'touchstart'].forEach((name) =>
    document.addEventListener(name, unlock, {capture: true, passive: true}));

  const stopCurrent = (reason) => {
    const run = current;
    if (!run) return;
    current = null;
    run.aborted = true;
    try { run.abort.abort(); } catch (_) {}
    run.sources.forEach((source) => { try { source.stop(); } catch (_) {} });
    run.sources.clear();
    note(reason);
  };
  // A reply that plays out is done: clear `current` so rivaIsPlaying() goes false. It is NOT
  // enough to wait for sources to drain via onended - a single missed onended (clock drift, a
  // dropped event, a suspend/resume) would leave current.sources non-empty and rivaIsPlaying()
  // stuck true, which keeps the VAD's deaf-while-playing guard on forever.
  const finish = (run, tag) => {
    if (run.done) return;
    run.done = true;
    run.sources.forEach((source) => { try { source.stop(); } catch (_) {} });
    run.sources.clear();
    if (current === run) current = null;
    if (!run.aborted) note(tag || 'ended');
  };
  // Watchdog: once the stream has fully arrived, clear `current` when the scheduled end time
  // passes even if some onended never fires. While the context is suspended (barge-in hold) the
  // audio clock is frozen, so we wait rather than finish early.
  const watchEnd = (run) => {
    if (run.done || run.aborted || current !== run) return;
    if (!run.sources.size) { finish(run); return; }
    // The scheduled end passed but sources are still registered: an onended was missed, which is
    // exactly what would have stuck rivaIsPlaying() true. Clear it and flag it distinctly.
    if (ctx && ctx.state === 'running' && ctx.currentTime >= run.next - 0.05) {
      finish(run, 'ended_watchdog');
    } else setTimeout(() => watchEnd(run), 120);
  };
  const schedule = (run, bytes) => {
    const count = bytes.length / 2;
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
    const buffer = ctx.createBuffer(1, count, RATE);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < count; i++) channel[i] = view.getInt16(i * 2, true) / 32768;
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    const when = Math.max(run.next, ctx.currentTime + 0.08);
    source.start(when);
    stats.chunks += 1;
    run.next = when + buffer.duration;
    run.sources.add(source);
    source.onended = () => {
      run.sources.delete(source);
      if (run.finished && !run.sources.size) finish(run);
    };
    if (!run.started) {
      run.started = true;
      note('first_chunk');
      fetch('/riva/playback', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({generation_id: run.id, now_ms: performance.now()})
      }).catch(() => {});
    }
  };
  const play = async (id) => {
    stopCurrent('replaced');
    if (userSpeaking()) { note('dropped_user_speaking'); return; }
    ensureContext();
    if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (_) {} }
    const run = {id: id, abort: new AbortController(), sources: new Set(), next: 0,
                 started: false, finished: false, aborted: false, suspended: false, done: false};
    current = run;
    note('stream_started');
    try {
      const response = await fetch('/riva/reply_audio/' + id, {
        signal: run.abort.signal, cache: 'no-store'});
      if (!response.ok || !response.body) { note('fetch_http_' + response.status); return; }
      const reader = response.body.getReader();
      let carry = new Uint8Array(0);
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        let bytes = value;
        if (carry.length) {
          const merged = new Uint8Array(carry.length + bytes.length);
          merged.set(carry);
          merged.set(bytes, carry.length);
          bytes = merged;
        }
        const usable = bytes.length - (bytes.length % 2);
        carry = bytes.slice(usable);
        if (usable && !run.aborted) schedule(run, bytes.subarray(0, usable));
      }
      run.finished = true;
      watchEnd(run);
    } catch (error) {
      if (!run.aborted) note('stream_error_' + String((error && error.name) || 'Error'));
    }
  };

  // Barge-in, driven by the browser VAD state (see VAD_BROWSER_JS in asr_stream.py).
  window.rivaHoldReplyAudio = () => {
    if (!current || !ctx || ctx.state !== 'running') return;
    current.suspended = true;
    ctx.suspend().then(() => note('suspended_user_speaking')).catch(() => {});
  };
  // For the participant page: is a reply audibly playing, and the Stopp button.
  window.rivaIsPlaying = () =>
    Boolean(current && ctx && ctx.state === 'running' && current.sources.size > 0);
  window.rivaStopSpeech = () => { stopCurrent('stopped_by_user'); };
  window.rivaDropReplyAudio = () => {  // a real speech segment ended: the old reply is stale
    const wasSuspended = Boolean(current && current.suspended);
    stopCurrent('dropped_stale_reply');
    if (wasSuspended && ctx) ctx.resume().catch(() => {});
  };
  window.rivaResumeReplyAudio = () => {  // VAD misfire: carry on
    if (current && current.suspended && ctx) {
      current.suspended = false;
      ctx.resume().then(() => note('resumed_misfire')).catch(() => {});
    }
  };

  let lastGeneration = '';
  setInterval(() => {
    const field = document.querySelector('#riva-generation-id textarea, #riva-generation-id input');
    const id = field ? String(field.value || '').trim() : '';
    if (!/^[a-f0-9]{32}$/.test(id) || id === lastGeneration) return;
    lastGeneration = id;
    play(id);
  }, 150);
}
"""


def build_voice_api(
    demo: gr.Blocks,
    replies: ReplyManager,
    *,
    css: str | None = None,
    head: str | None = None,
    extra_js: str = "",
    footer_links: list[str] | None = None,
    extra_routes: Callable[[FastAPI], None] | None = None,
    extra_apps: list[tuple[str, gr.Blocks, dict[str, Any]]] | None = None,
) -> FastAPI:
    """The FastAPI app: the Gradio page plus the playback, reply-audio and diagnostics routes."""
    from gradio.routes import mount_gradio_app

    demo.queue(max_size=64)
    api = FastAPI()

    browser_events = {
        "hls_error": EventName.BROWSER_HLS_ERROR,
        "audio": EventName.BROWSER_AUDIO,
        "segment": EventName.BROWSER_SEGMENT,
    }

    @api.post("/riva/browser_event")
    async def record_browser_event(request: Request) -> dict[str, bool]:
        """Log an allowlisted browser diagnostic: event kind plus a short token."""
        try:
            data = await request.json()
            name = browser_events[data["event"]]
            write_event(Event(name, "browser", detail=data["detail"]), sys.stdout)
        except (ValueError, KeyError, TypeError):
            return {"recorded": False}
        return {"recorded": True}

    add_reply_audio_route(api, replies)
    if extra_routes is not None:
        extra_routes(api)  # must be registered before Gradio is mounted at "/"

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

    for path, extra_app, options in extra_apps or []:  # before "/", which would catch them
        mount_gradio_app(api, extra_app, path=path, **options)
    mounted: FastAPI = mount_gradio_app(
        api,
        demo,
        path="/",
        app_kwargs={},
        js=PLAYBACK_JS + extra_js,
        css=css,
        head=head,
        footer_links=footer_links,
        max_file_size="30mb",
        show_error=False,
    )
    return mounted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--private-dir", type=Path, default=Path(os.environ.get("RIVA_PRIVATE_DIR", "../private"))
    )
    parser.add_argument("--port", type=int, default=7861)
    parser.add_argument("--llm-model", default=os.environ.get("RIVA_LLM_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--tts-model", default=os.environ.get("RIVA_TTS_MODEL", "gpt-4o-mini-tts"))
    parser.add_argument("--voice", default=os.environ.get("RIVA_TTS_VOICE", "coral"))
    parser.add_argument(
        "--vad-self-test",
        action="store_true",
        help="Run a synthetic VAD-segment-to-reply harness with mocked providers and no API key.",
    )
    args = parser.parse_args()
    if not args.vad_self_test and not os.environ.get("OPENAI_API_KEY", "").strip():
        parser.error("OPENAI_API_KEY missing. Run: uv run --env-file .env riva-voice-test")
    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")
    root = prepare_environment(args.private_dir)
    if args.vad_self_test:
        from riva.experiments.vad_self_test import SyntheticReplyProvider, SyntheticWire

        sessions = AsrSessions(
            RecordingStore(root / "research-data" / "vad-self-test"),
            AsrConfig(),
            SyntheticWire,
        )
        replies = ReplyManager(ReplyConfig(), SyntheticReplyProvider)
    else:
        sessions = AsrSessions(RecordingStore(root / "research-data" / "voice-test"), AsrConfig())
        replies = ReplyManager(
            ReplyConfig(llm_model=args.llm_model, tts_model=args.tts_model, voice=args.voice)
        )
    demo = build_vad_app(sessions, replies, test_mode=args.vad_self_test)
    import uvicorn

    api = build_voice_api(demo, replies)
    if args.vad_self_test:
        print(
            f"Synthetic VAD self-test: http://localhost:{args.port}; no provider calls or API key"
        )
    else:
        print(f"Voice test: http://localhost:{args.port}; OpenAI ASR, LLM and TTS enabled")
        print("Speech API models are deprecated; announced shutdown 2027-01-06 (riva-ddc).")
        print("Browser playback timing uses a JavaScript observer.")
    uvicorn.run(api, host="127.0.0.1", port=args.port, log_level="warning")
