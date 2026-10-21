"""German streaming transcription experiment: native Gradio chunks → OpenAI WS."""

from __future__ import annotations

import argparse
import os
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from riva.audio.recordings import (
    AudioInput,
    RecordingStore,
    SavedRecording,
    SessionIdentity,
    new_development_session,
)
from riva.audio.transcription import AsrConfig, LiveTurn, Transcript, Wire, connect_openai
from riva.audio.transcripts import save_transcript
from riva.event_log import Event, EventName, write_event
from riva.experiments.audio_capture import prepare_environment

if TYPE_CHECKING:
    import gradio as gr


@dataclass
class SessionTurn:
    live: LiveTurn
    stopped: bool = False
    saved: SavedRecording | None = None
    persisted: bool = False


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

    def append(self, audio: AudioInput, identity: SessionIdentity) -> None:
        turn = self.current(identity)
        if turn.stopped:
            turn.live.fail("audio_after_stop")
            turn.live.cancelled.set()
            raise ValueError("Audio arrived after the stop boundary")
        turn.live.append(audio)

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


def build_asr_app(sessions: AsrSessions) -> gr.Blocks:
    import gradio as gr

    def begin(identity: SessionIdentity) -> gr.Button:
        try:
            sessions.begin(identity)
        except ValueError:
            raise gr.Error(
                "Bitte die vorherige Aufnahme zuerst abschließen und speichern."
            ) from None
        return gr.Button(interactive=False)

    def append(audio: AudioInput | None, identity: SessionIdentity) -> None:
        if audio is None:
            return
        try:
            sessions.append(audio, identity)
        except (ValueError, KeyError):
            raise gr.Error(
                "Audiostream abgelehnt: Reihenfolge, Format oder Aufnahmelimit prüfen."
            ) from None

    def stopped(identity: SessionIdentity) -> gr.Button:
        with suppress(KeyError):
            sessions.current(identity).stopped = True
        return gr.Button(interactive=True)

    def poll(identity: SessionIdentity) -> tuple[str, str, str]:
        result, duration = sessions.snapshot(identity)
        detail = f"{result.status} · empfangen: {duration:.2f} s"
        if result.error_code:
            detail += f" · {result.error_code} (Audio bitte trotzdem speichern)"
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
        sessions.release(identity)
        return gr.Audio(value=None, recording=False), gr.Button(interactive=False)

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
            "Keine Wort-Zeitstempel/SRT, keine automatische Gesprächsantwort."
        )
        identity = gr.State(
            value=new_development_session, time_to_live=600, delete_callback=sessions.release
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
        finish_button.click(
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
    )
