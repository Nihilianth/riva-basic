"""German streaming transcription experiment: native Gradio chunks → OpenAI WS."""

import argparse
import base64
import binascii
import json
import os
import re
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from starlette.middleware import Middleware

from riva.audio.recordings import (
    AudioInput,
    RecordingStore,
    SavedRecording,
    SessionIdentity,
    new_development_session,
)
from riva.audio.replies import ReplyManager
from riva.audio.transcription import AsrConfig, LiveTurn, Transcript, Wire, connect_openai
from riva.audio.transcripts import save_transcript
from riva.event_log import Event, EventName, write_event
from riva.experiments.audio_capture import prepare_environment
from riva.experiments.gradio_delivery import DELIVERY_FIELD, AudioDeliveryMiddleware

if TYPE_CHECKING:
    import gradio as gr


@dataclass
class SessionTurn:
    live: LiveTurn
    stopped: bool = False
    saved: SavedRecording | None = None
    persisted: bool = False
    delivery_ids: set[str] = field(default_factory=set)
    replays_skipped: int = 0


class AsrSessions:
    def __init__(
        self,
        store: RecordingStore,
        config: AsrConfig,
        factory: Callable[[], Wire] = connect_openai,
    ) -> None:
        self.store, self.config, self.factory = store, config, factory
        self.lock = threading.RLock()
        self.turns: dict[str, SessionTurn] = {}

    def begin(self, identity: SessionIdentity) -> None:
        with self.lock:
            old = self.turns.get(identity.session_id)
            if old is not None and not old.persisted:
                raise ValueError("Finish/save the previous turn first")
            if old is not None:
                old.live.close()
            if old is None and len(self.turns) >= 8:
                raise ValueError("Too many development sessions; close unused sessions")
            self.turns[identity.session_id] = SessionTurn(LiveTurn(self.config, self.factory))

    def current(self, identity: SessionIdentity) -> SessionTurn:
        with self.lock:
            return self.turns[identity.session_id]

    def append(
        self,
        audio: AudioInput,
        identity: SessionIdentity,
        *,
        delivery_id: str | None = None,
    ) -> None:
        turn = self.current(identity)
        with self.lock:
            if delivery_id is not None:
                if not re.fullmatch(r"[a-f0-9]{32}", delivery_id):
                    raise ValueError("Invalid delivery identity")
                if delivery_id in turn.delivery_ids:
                    turn.replays_skipped += 1
                    write_event(
                        Event(EventName.STREAM_REPLAY_SKIPPED, identity.session_id), sys.stdout
                    )
                    return
            if turn.stopped:
                turn.live.fail("audio_after_stop")
                turn.live.cancelled.set()
                raise ValueError("Audio arrived after the stop boundary")
            turn.live.append(audio)
            if delivery_id is not None:
                turn.delivery_ids.add(delivery_id)

    def finish(self, identity: SessionIdentity) -> SavedRecording:
        turn = self.current(identity)
        if not turn.stopped:
            raise ValueError("Stop microphone recording before finishing")
        if turn.saved is None:
            # Save the exact received stream first, even if the provider is down.
            turn.saved = self.store.save(turn.live.audio(), identity)
            write_event(
                Event(EventName.RECORDING_SAVED, identity.session_id, turn.saved.metadata.turn_id),
                sys.stdout,
            )
        result = turn.live.finish()
        if not turn.persisted:
            save_transcript(turn.saved, result, self.config)
            turn.persisted = True
            name = (
                EventName.ASR_FINALIZED
                if result.status == "completed"
                else EventName.PROVIDER_FAILED
            )
            write_event(Event(name, identity.session_id, turn.saved.metadata.turn_id), sys.stdout)
        return turn.saved

    def snapshot(self, identity: SessionIdentity) -> tuple[Transcript, float]:
        try:
            turn = self.current(identity)
        except KeyError:
            return Transcript("ready"), 0
        with turn.live.lock:
            duration = turn.live.frames / turn.live.source_rate if turn.live.source_rate else 0
        return turn.live.snapshot(), duration

    def release(self, identity: SessionIdentity) -> None:
        with self.lock:
            turn = self.turns.pop(identity.session_id, None)
        if turn is not None:
            turn.live.close()


def asr_app_kwargs(app: "gr.Blocks") -> dict[str, Any]:
    """Pass at launch: Gradio recreates its FastAPI app when launching."""
    index = next(i for i, fn in app.fns.items() if fn.fn and fn.fn.__name__ == "append")
    return {"middleware": [Middleware(AudioDeliveryMiddleware, fn_index=index)]}


def build_asr_app(sessions: AsrSessions, replies: ReplyManager | None = None) -> "gr.Blocks":
    import gradio as gr

    vad_js = r"""
async (enabled) => {
  const status = (message) => {
    const nodes = document.querySelectorAll("[data-testid='markdown']");
    for (const node of nodes) {
      if (node.textContent && node.textContent.includes("VAD is")) node.textContent = message;
    }
  };
  if (!window.rivaVad) {
    window.rivaVad = {instance: null, loading: null, enabled: false, busy: false, pending: []};
  }
  if (!enabled) {
    window.rivaVad.enabled = false;
    window.rivaVad.pending = [];
    if (window.rivaVad.instance) window.rivaVad.instance.pause();
    status("VAD is inactive (manual mode).");
    return;
  }
  if (!window.rivaVad.loading) {
    window.rivaVad.loading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.14.0/dist/ort.js";
      script.onload = () => {
        const vadScript = document.createElement("script");
        vadScript.src = "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.27/dist/bundle.min.js";
        vadScript.onload = resolve;
        vadScript.onerror = reject;
        document.head.appendChild(vadScript);
      };
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }
  try {
    await window.rivaVad.loading;
    if (!window.vad || !window.vad.MicVAD) throw new Error("VAD library unavailable");
    if (window.rivaVad.instance) window.rivaVad.instance.destroy();
    const submitNext = () => {
      if (!window.rivaVad.enabled || window.rivaVad.busy || !window.rivaVad.pending.length) return;
      window.rivaVad.busy = true;
      window.rivaVad.activePayload = window.rivaVad.pending.shift();
      const button = document.querySelector("#riva-vad-submit button");
      if (button) button.click();
      else {
        window.rivaVad.busy = false;
        status("VAD segment submission control is unavailable.");
      }
    };
    window.rivaVad.submitNext = submitNext;
    const vadBase = "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.27/dist/";
    const ortBase = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.14.0/dist/";
    window.rivaVad.instance = await window.vad.MicVAD.new({
      model: "v5",
      baseAssetPath: vadBase,
      onnxWASMBasePath: ortBase,
      redemptionMs: 900,
      preSpeechPadMs: 300,
      minSpeechMs: 250,
      onSpeechStart: () => status("Speech detected…"),
      onSpeechEnd: (audio) => {
        if (!window.rivaVad.enabled) return;
        if (window.rivaVad.pending.length >= 2) {
          status("VAD busy; segment limit reached. Wait for the current reply.");
          return;
        }
        const pcm = new Int16Array(audio.length);
        for (let i = 0; i < audio.length; i++) {
          const sample = Math.max(-1, Math.min(1, audio[i]));
          pcm[i] = sample < 0 ? sample * 32768 : sample * 32767;
        }
        const bytes = new Uint8Array(pcm.buffer);
        let binary = "";
        for (let i = 0; i < bytes.length; i += 0x8000) {
          binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        }
        window.rivaVad.pending.push(JSON.stringify({
          pcm16_b64: btoa(binary),
          started_ms: performance.now(),
        }));
        status("Speech segment captured; processing…");
        submitNext();
      },
      onVADMisfire: () => {},
    });
    window.rivaVad.enabled = true;
    await window.rivaVad.instance.start();
    status("VAD is active in the browser.");
  } catch (error) {
    window.rivaVad.enabled = false;
    status("VAD could not start; use manual recording.");
    console.warn("RIVA VAD unavailable", error);
  }
}
"""

    def begin(identity: SessionIdentity) -> gr.Button:
        if replies is not None:
            run = replies.current(identity)
            if run is not None and not run.done.is_set():
                raise gr.Error("Bitte warten, bis die Antwort erzeugt wurde.")
        try:
            sessions.begin(identity)
        except ValueError:
            raise gr.Error(
                "Bitte die vorherige Aufnahme zuerst abschließen und speichern."
            ) from None
        return gr.Button(interactive=False)

    def append(audio: AudioInput | None, identity: SessionIdentity, event: gr.EventData) -> None:
        if audio is None:
            return
        try:
            data = event._data
            delivery_id = data.get(DELIVERY_FIELD) if isinstance(data, dict) else None
            if not isinstance(delivery_id, str):
                raise ValueError("Missing delivery tracking middleware")
            sessions.append(audio, identity, delivery_id=delivery_id)
        except (ValueError, KeyError):
            raise gr.Error(
                "Audiostream abgelehnt: Reihenfolge, Format oder Aufnahmelimit prüfen."
            ) from None

    def stopped(identity: SessionIdentity) -> gr.Button:
        with suppress(KeyError):
            sessions.current(identity).stopped = True
        return gr.Button(interactive=True)

    def vad_segment(payload: str, identity: SessionIdentity) -> Iterator[tuple[Any, ...]]:
        """Decode one VAD-owned PCM segment and run isolated ASR then optional reply."""
        try:
            if not isinstance(payload, str) or len(payload) > 1_400_000:
                raise ValueError("invalid_segment")
            data = json.loads(payload)
            if not isinstance(data, dict) or set(data) != {"pcm16_b64", "started_ms"}:
                raise ValueError("invalid_segment")
            encoded = data["pcm16_b64"]
            if not isinstance(encoded, str):
                raise ValueError("invalid_segment")
            pcm = base64.b64decode(encoded, validate=True)
            if len(pcm) % 2 or not 3200 <= len(pcm) <= 960_000:
                raise ValueError("invalid_segment")
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            started_ms = data["started_ms"]
            if isinstance(started_ms, bool) or not isinstance(started_ms, (int, float)):
                raise ValueError("invalid_segment")
            if not np.isfinite(started_ms) or started_ms < 0:
                raise ValueError("invalid_segment")
            sessions.begin(identity)
            sessions.append((16000, samples), identity)
            sessions.current(identity).stopped = True
            saved = sessions.finish(identity)
            result, duration = sessions.snapshot(identity)
            detail = f"VAD segment saved · {duration:.2f} s · ASR {result.status}"
        except (KeyError, ValueError, OSError, binascii.Error):
            yield "", "", "VAD segment rejected or could not be processed", {}, "", None, {}, ""
            return
        run = None
        if replies is not None and result.status == "completed" and result.text:
            try:
                run, created = replies.start(identity, saved, result.text, float(started_ms))
            except (ValueError, OSError):
                run = None
                detail += " · Antwort konnte nicht gestartet werden"
        metadata = {**asdict(saved.metadata), "transcription_status": result.status}
        yield result.partial, result.text or "", detail, metadata, "", None, {}, ""
        if run is not None and created:
            write_event(
                Event(
                    EventName.RESPONSE_STARTED,
                    identity.session_id,
                    saved.metadata.turn_id,
                    run.generation_id,
                ),
                sys.stdout,
            )
            for reply_text, wav, metrics in run.updates():
                yield (
                    result.partial,
                    result.text or "",
                    f"VAD segment saved · {duration:.2f} s · reply {metrics['status']}",
                    metadata,
                    reply_text,
                    wav,
                    metrics,
                    "",
                )
            event_name = (
                EventName.RESPONSE_COMPLETED
                if run.status == "completed"
                else EventName.RESPONSE_FAILED
            )
            write_event(
                Event(event_name, identity.session_id, saved.metadata.turn_id, run.generation_id),
                sys.stdout,
            )

    def poll(identity: SessionIdentity) -> tuple[str, str, str]:
        result, duration = sessions.snapshot(identity)
        detail = f"{result.status} · empfangen: {duration:.2f} s"
        if result.error_code:
            detail += f" · {result.error_code} (Audio bitte trotzdem speichern)"
        with suppress(KeyError):
            skipped = sessions.current(identity).replays_skipped
            if skipped:
                detail += f" · doppelte Zustellungen übersprungen: {skipped}"
        return result.partial, result.text or "", detail

    def finish(identity: SessionIdentity) -> tuple[str, dict[str, Any]]:
        try:
            saved = sessions.finish(identity)
            cached = playback.move_resource_to_block_cache(saved.audio_path)
            if cached is None or not Path(cached).resolve().is_relative_to(
                Path(playback.GRADIO_CACHE).resolve()
            ):
                raise ValueError("Invalid playback cache")
        except (ValueError, KeyError, OSError, RuntimeError):
            raise gr.Error(
                "Abschließen fehlgeschlagen. Aufnahme stoppen und Speicher prüfen."
            ) from None
        result, _ = sessions.snapshot(identity)
        return cached, {**asdict(saved.metadata), "transcription_status": result.status}

    def discard(identity: SessionIdentity) -> tuple[gr.Audio, gr.Button]:
        release(identity)
        return gr.Audio(value=None, recording=False), gr.Button(interactive=False)

    def release(identity: SessionIdentity) -> None:
        sessions.release(identity)
        if replies is not None:
            replies.release(identity)

    app = gr.Blocks(analytics_enabled=False, delete_cache=(300, 3600))
    with app:
        gr.Markdown(
            "# RIVA – Streaming-ASR (Deutsch)\n"
            "**Lokaler Entwicklungstest, keine Teilnehmerkonten.** "
            "Mikrofon-Audio wird während der Aufnahme an OpenAI gesendet (API-Kosten).\n\n"
            "Aufnehmen → nach dem letzten Wort kurz pausieren → Mikrofon stoppen → "
            "**Abschließen & speichern**. Vor einer neuen Aufnahme bitte immer speichern.\n\n"
            "Manual mode speichert die vom Gradio-Mikrofon empfangenen Chunks. "
            "Gradio kann beim Stoppen den letzten noch nicht gesendeten Chunk verwerfen. "
            "Keine Wort-Zeitstempel/SRT, keine automatische Gesprächsantwort.\n\n"
            "Continuous mode uses browser VAD-owned speech segments (16 kHz PCM16, up to 30 s); "
            "each detected segment is saved and transcribed automatically. In the voice app, "
            "completed segments also trigger a streamed AI reply. "
            "Disable this mode to stop listening."
        )
        identity = gr.State(
            value=new_development_session, time_to_live=600, delete_callback=release
        )
        microphone = gr.Audio(
            sources=["microphone"],
            streaming=True,
            type="numpy",
            format="wav",
            buttons=[],
            label="Live-Mikrofon",
        )
        finish_button = gr.Button("Abschließen & speichern", interactive=False, variant="primary")
        continuous_mode = gr.Checkbox(
            label="Continuous listening (experimental)",
            value=False,
            info=(
                "Browser VAD submits each detected speech segment for private saving and ASR; "
                "the voice app also replies automatically."
            ),
        )
        vad_status = gr.Markdown("VAD is inactive (manual mode).")
        vad_payload = gr.Textbox(visible=False)
        vad_submit = gr.Button("Submit VAD segment", visible=False, elem_id="riva-vad-submit")
        discard_button = gr.Button("Ungespeicherte Aufnahme verwerfen")
        partial = gr.Textbox(label="Vorläufige Transkription", interactive=False)
        final = gr.Textbox(label="Endgültige Transkription", interactive=False)
        status = gr.Textbox(label="Status", interactive=False)
        playback = gr.Audio(label="Empfangenes Audio anhören", interactive=False, buttons=[])
        details = gr.JSON(label="Gespeicherte Aufnahmedaten")
        vad_reply_text = gr.Textbox(
            label="KI-Antwort (VAD)", interactive=False, visible=replies is not None
        )
        vad_reply_audio = gr.Audio(
            label="KI-Stimme (VAD)",
            streaming=True,
            autoplay=True,
            interactive=False,
            buttons=[],
            visible=replies is not None,
        )
        vad_reply_metrics = gr.JSON(label="Antwortstatus (VAD)", visible=replies is not None)
        vad_reply_latency = gr.Markdown(visible=replies is not None)
        timer = gr.Timer(0.25)
        group = "asr-input"
        microphone.start_recording(
            begin,
            inputs=[identity],
            outputs=[finish_button],
            concurrency_id=group,
            concurrency_limit=1,
            api_visibility="private",
        )
        microphone.stream(
            append,
            inputs=[microphone, identity],
            stream_every=0.5,
            time_limit=180,
            concurrency_id=group,
            concurrency_limit=1,
            trigger_mode="multiple",
            api_visibility="private",
        )
        microphone.stop_recording(
            stopped,
            inputs=[identity],
            outputs=[finish_button],
            concurrency_id=group,
            concurrency_limit=1,
            api_visibility="private",
        )
        continuous_mode.change(
            lambda enabled: (
                gr.Button(interactive=not enabled),
                "VAD is enabled; detected speech segments are saved and transcribed separately."
                if enabled
                else "VAD is inactive (manual mode).",
                gr.Audio(visible=not enabled),
            ),
            inputs=[continuous_mode],
            outputs=[finish_button, vad_status, microphone],
            api_visibility="private",
        )
        continuous_mode.change(
            None,
            inputs=[continuous_mode],
            outputs=[],
            js=vad_js,
            queue=False,
            api_visibility="private",
        )
        vad_submit.click(
            vad_segment,
            inputs=[vad_payload, identity],
            outputs=[
                partial,
                final,
                status,
                details,
                vad_reply_text,
                vad_reply_audio,
                vad_reply_metrics,
                vad_reply_latency,
            ],
            js="(payload, identity) => [window.rivaVad.activePayload || '', identity]",
            concurrency_limit=1,
            api_visibility="private",
        ).then(
            None,
            inputs=[],
            outputs=[],
            js=r"""
() => {
  if (!window.rivaVad) return;
  window.rivaVad.activePayload = null;
  window.rivaVad.busy = false;
  if (window.rivaVad.submitNext) window.rivaVad.submitNext();
}
""",
            queue=False,
            api_visibility="private",
        )
        finish_event = finish_button.click(
            finish,
            inputs=[identity],
            outputs=[playback, details],
            concurrency_id=group,
            concurrency_limit=1,
            api_visibility="private",
        )
        timer.tick(
            poll,
            inputs=[identity],
            outputs=[partial, final, status],
            queue=False,
            api_visibility="private",
        )
        discard_button.click(
            discard,
            inputs=[identity],
            outputs=[microphone, finish_button],
            concurrency_id=group,
            concurrency_limit=1,
            api_visibility="private",
        )
        if replies is not None:
            from riva.experiments.voice_reply import add_reply_controls

            add_reply_controls(sessions, replies, identity, finish_event)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--private-dir", type=Path, default=Path(os.environ.get("RIVA_PRIVATE_DIR", "../private"))
    )
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument(
        "--model", choices=["gpt-live-transcribe", "gpt-transcribe"], default="gpt-live-transcribe"
    )
    parser.add_argument(
        "--delay", choices=["minimal", "low", "medium", "high", "xhigh"], default="low"
    )
    args = parser.parse_args()
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        parser.error("OPENAI_API_KEY missing. Run: uv run --env-file .env riva-asr-test")
    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")
    root = prepare_environment(args.private_dir)
    config = AsrConfig(model=args.model, delay=args.delay)
    sessions = AsrSessions(RecordingStore(root / "research-data" / "asr-test"), config)
    app = build_asr_app(sessions)
    print(f"ASR test: http://localhost:{args.port}; OpenAI audio enabled; model={config.model}")
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
