"""German streaming transcription experiment: native Gradio chunks → OpenAI WS."""

import argparse
import os
import re
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
async () => {
  const status = (message) => {
    const nodes = document.querySelectorAll("[data-testid='markdown']");
    for (const node of nodes) {
      if (node.textContent && node.textContent.includes("VAD is")) node.textContent = message;
    }
  };
  if (!window.rivaVad) {
    window.rivaVad = {instance: null, loading: null, enabled: false};
  }
  if (!window.rivaVad.loading) {
    window.rivaVad.loading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.27/dist/bundle.min.js";
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }
  try {
    await window.rivaVad.loading;
    if (!window.vad || !window.vad.MicVAD) throw new Error("VAD library unavailable");
    if (window.rivaVad.instance) window.rivaVad.instance.destroy();
    const vadBase = "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.27/dist/";
    const ortBase = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.14.0/dist/";
    window.rivaVad.instance = await window.vad.MicVAD.new({
      model: "v5",
      baseAssetPath: vadBase,
      onnxWASMBasePath: ortBase,
      onSpeechStart: () => {
        const button = [...document.querySelectorAll("button")].find(
          (b) => b.textContent.includes("Start recording")
        );
        if (button) button.click();
      },
      onSpeechEnd: () => {
        const button = [...document.querySelectorAll("button")].find(
          (b) => b.textContent.includes("Stop recording")
        );
        if (button) button.click();
      },
      onVADMisfire: () => {},
    });
    await window.rivaVad.instance.start();
    window.rivaVad.enabled = true;
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

    def continuous_stopped(identity: SessionIdentity, enabled: bool) -> gr.Button:
        """Mark an endpoint without starting a new turn or saving it."""
        if not enabled:
            return gr.Button(interactive=True)
        with suppress(KeyError):
            sessions.current(identity).stopped = True
        return gr.Button(interactive=True)

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
            "Gespeichert wird nur das vom Browser empfangene Audio. "
            "Gradio kann beim Stoppen den letzten noch nicht gesendeten Chunk verwerfen. "
            "Keine Wort-Zeitstempel/SRT, keine automatische Gesprächsantwort.\n\n"
            "Continuous listening ist experimentell: die derzeitige native Gradio-"
            "Aufnahme beendet weiterhin nur über Stop; zuverlässige clientseitige VAD-"
            "Endpoint-Erkennung wird separat untersucht."
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
            info="VAD stops the current turn after speech ends; explicit save remains required.",
        )
        vad_status = gr.Markdown("VAD is inactive (manual mode).")
        discard_button = gr.Button("Ungespeicherte Aufnahme verwerfen")
        partial = gr.Textbox(label="Vorläufige Transkription", interactive=False)
        final = gr.Textbox(label="Endgültige Transkription", interactive=False)
        status = gr.Textbox(label="Status", interactive=False)
        playback = gr.Audio(label="Empfangenes Audio anhören", interactive=False, buttons=[])
        details = gr.JSON(label="Gespeicherte Aufnahmedaten")
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
                "VAD is enabled in the browser. Speak normally; silence stops the current turn."
                if enabled
                else "VAD is inactive (manual mode).",
            ),
            inputs=[continuous_mode],
            outputs=[finish_button, vad_status],
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
        microphone.stop_recording(
            continuous_stopped,
            inputs=[identity, continuous_mode],
            outputs=[finish_button],
            trigger_mode="multiple",
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
