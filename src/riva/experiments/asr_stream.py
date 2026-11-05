"""German streaming transcription experiment: native Gradio chunks → OpenAI WS."""

import argparse
import base64
import binascii
import json
import math
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Generator, Iterator
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


SPEECH_STATE_TTL_SECONDS = 35.0  # longer than the 30 s segment cap: a closed tab cannot wedge it
TRANSCRIPT_LIMIT = 8000  # matches ReplyRun's bound on a transcript


@dataclass
class SpeechState:
    """What the browser VAD reported about the user's speech (browser clock in ms)."""

    speaking: bool = False
    last_event_ms: float = -1.0
    last_start_ms: float = -1.0
    updated: float = 0.0
    held: list[str] = field(default_factory=list)


def merge_transcripts(pieces: list[str], limit: int = TRANSCRIPT_LIMIT) -> str:
    """Join unanswered transcripts into one turn, dropping the oldest if over the bound."""
    kept = [piece.strip() for piece in pieces if piece.strip()]
    while len(kept) > 1 and len(" ".join(kept)) > limit:
        kept.pop(0)
    return " ".join(kept)


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
        self.vad_diagnostics: dict[str, list[dict[str, Any]]] = {}
        self.speech: dict[str, SpeechState] = {}

    def note_speech(self, identity: SessionIdentity, kind: str, at_ms: float) -> None:
        """Record a browser speech start/end; events older than the newest one are ignored."""
        if kind not in ("start", "end") or not math.isfinite(at_ms) or at_ms < 0:
            raise ValueError("invalid speech event")
        with self.lock:
            state = self.speech.setdefault(identity.session_id, SpeechState())
            if at_ms < state.last_event_ms:
                return
            state.last_event_ms = at_ms
            state.speaking = kind == "start"
            if kind == "start":
                state.last_start_ms = at_ms
            state.updated = time.monotonic()

    def newer_speech(self, identity: SessionIdentity, segment_end_ms: float | None) -> bool:
        """True while the user is speaking, or started speaking after the given segment ended."""
        with self.lock:
            state = self.speech.get(identity.session_id)
            if state is None:
                return False
            if state.speaking and time.monotonic() - state.updated <= SPEECH_STATE_TTL_SECONDS:
                return True
            return segment_end_ms is not None and state.last_start_ms > segment_end_ms

    def hold(self, identity: SessionIdentity, pieces: list[str], *, front: bool = False) -> None:
        """Keep transcripts that have not been answered yet for the user's next turn."""
        with self.lock:
            state = self.speech.setdefault(identity.session_id, SpeechState())
            state.held = [*pieces, *state.held] if front else [*state.held, *pieces]

    def held_preview(self, identity: SessionIdentity) -> list[str]:
        with self.lock:
            state = self.speech.get(identity.session_id)
            return list(state.held) if state else []

    def take_held(self, identity: SessionIdentity) -> list[str]:
        with self.lock:
            state = self.speech.get(identity.session_id)
            if state is None:
                return []
            held, state.held = state.held, []
            return held

    def update_vad_diagnostics(
        self, identity: SessionIdentity, events: list[dict[str, Any]]
    ) -> None:
        with self.lock:
            self.vad_diagnostics[identity.session_id] = [dict(event) for event in events]

    def get_vad_diagnostics(self, identity: SessionIdentity) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(event) for event in self.vad_diagnostics.get(identity.session_id, [])]

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
            self.vad_diagnostics.pop(identity.session_id, None)
            self.speech.pop(identity.session_id, None)
        if turn is not None:
            turn.live.close()


def asr_app_kwargs(app: "gr.Blocks") -> dict[str, Any]:
    """Pass at launch: Gradio recreates its FastAPI app when launching."""
    index = next(i for i, fn in app.fns.items() if fn.fn and fn.fn.__name__ == "append")
    return {"middleware": [Middleware(AudioDeliveryMiddleware, fn_index=index)]}


class SegmentTimeline:
    """Allowlisted diagnostic stages for one segment, mirrored into the session for the UI poll."""

    def __init__(self, sessions: "AsrSessions", identity: SessionIdentity) -> None:
        self.sessions, self.identity = sessions, identity
        self.started = time.monotonic()
        self.rows: list[dict[str, Any]] = []
        sessions.update_vad_diagnostics(identity, self.rows)

    def record(self, stage: str, source: str = "server", elapsed_ms: float | None = None) -> None:
        self.rows.append(
            {
                "stage": stage,
                "source": source,
                "elapsed_ms": round(elapsed_ms, 1)
                if elapsed_ms is not None
                else round((time.monotonic() - self.started) * 1000, 1),
            }
        )
        self.sessions.update_vad_diagnostics(self.identity, self.rows)


@dataclass(frozen=True)
class SegmentProgress:
    """What the UI may show while a segment is processed (no audio, no provider errors)."""

    partial: str = ""
    final: str = ""
    status: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SegmentOutcome:
    saved: SavedRecording
    result: Transcript
    duration: float
    started_ms: float
    segment_end_ms: float | None
    synthetic: bool


def process_segment(
    payload: str,
    identity: SessionIdentity,
    sessions: "AsrSessions",
    timeline: SegmentTimeline,
    *,
    allow_synthetic: bool = False,
) -> Generator[SegmentProgress, None, SegmentOutcome | None]:
    """Validate, privately save and transcribe one browser-VAD 16 kHz PCM16 segment.

    Yields progress for the UI; returns the outcome, or ``None`` when the segment was rejected
    or could not be persisted (the last progress then carries the German-neutral status text).
    Everything after the transcript (reply, LLM turn) belongs to the caller.
    """
    record = timeline.record
    segment_end_ms: float | None = None
    try:
        if not isinstance(payload, str) or len(payload) > 1_400_000:
            raise ValueError("invalid_segment")
        data = json.loads(payload)
        allowed_keys = {"pcm16_b64", "started_ms", "events"}
        if allow_synthetic:
            allowed_keys.add("synthetic")
        if not isinstance(data, dict) or set(data) != allowed_keys:
            raise ValueError("invalid_segment")
        if bool(data.get("synthetic", False)) != allow_synthetic:
            raise ValueError("invalid_segment")
        synthetic = allow_synthetic
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
        browser_events = data["events"]
        allowed_stages = {
            "vad.speech_start",
            "vad.speech_end",
            "vad.segment_queued",
            "vad.submit_clicked",
            "harness.injected",
        }
        if not isinstance(browser_events, list) or len(browser_events) > 8:
            raise ValueError("invalid_segment")
        for browser_event in browser_events:
            if not isinstance(browser_event, dict) or set(browser_event) != {"stage", "at_ms"}:
                raise ValueError("invalid_segment")
            if browser_event["stage"] not in allowed_stages:
                raise ValueError("invalid_segment")
            at_ms = browser_event["at_ms"]
            if isinstance(at_ms, bool) or not isinstance(at_ms, (int, float)):
                raise ValueError("invalid_segment")
            if not np.isfinite(at_ms):
                raise ValueError("invalid_segment")
            if browser_event["stage"] == "vad.speech_end":
                segment_end_ms = float(at_ms)
            record(str(browser_event["stage"]), "browser", float(at_ms) - float(started_ms))
        record("segment.event_received")
        record("segment.validated")
        sessions.begin(identity)
        sessions.append((16000, samples), identity)
        sessions.current(identity).stopped = True
        record("asr.turn_accepted")
    except (KeyError, ValueError, OSError, binascii.Error):
        record("segment.rejected")
        yield SegmentProgress(status="VAD segment rejected or could not be processed")
        return None

    finished = threading.Event()
    finish_result: dict[str, Any] = {}

    def finish_turn() -> None:
        try:
            finish_result["saved"] = sessions.finish(identity)
        except Exception:
            finish_result["error"] = "segment_processing_failed"
        finally:
            finished.set()

    record("asr.connection_starting")
    finish_thread = threading.Thread(target=finish_turn, daemon=True, name="riva-vad-turn")
    finish_thread.start()
    last_asr_status = "connecting"
    audio_sent_logged = False
    commit_sent_logged = False
    while True:
        result, duration = sessions.snapshot(identity)
        turn = sessions.current(identity)
        if turn.live.audio_sent.is_set() and not audio_sent_logged:
            audio_sent_logged = True
            record("asr.audio_sent")
        if turn.live.commit_sent.is_set() and not commit_sent_logged:
            commit_sent_logged = True
            record("asr.commit_sent")
        if result.status != last_asr_status:
            last_asr_status = result.status
            stage = {
                "listening": "asr.session_ready",
                "finalizing": "asr.finalizing",
                "completed": "asr.completed",
                "failed": "asr.failed",
            }.get(result.status)
            if stage:
                record(stage)
            saved_during_turn = sessions.current(identity).saved
            metadata = asdict(saved_during_turn.metadata) if saved_during_turn else {}
            yield SegmentProgress(
                result.partial,
                result.text or "",
                f"ASR {result.status} · segment {duration:.2f} s",
                metadata,
            )
        if finished.is_set():
            break
        finished.wait(0.1)
    finish_thread.join(timeout=0)
    if "error" in finish_result:
        record("asr.persist_failed")
        yield SegmentProgress(status="VAD segment could not be persisted")
        return None
    saved = finish_result["saved"]
    result, duration = sessions.snapshot(identity)
    record("recording.persisted")
    return SegmentOutcome(saved, result, duration, float(started_ms), segment_end_ms, synthetic)


@dataclass(frozen=True)
class TurnDecision:
    """Hold the transcript for later, or answer now with everything not yet answered merged."""

    hold: bool
    merged: str = ""
    previous: tuple[str, ...] = ()


def gate_transcript(
    sessions: "AsrSessions",
    identity: SessionIdentity,
    text: str,
    segment_end_ms: float | None,
    saved: SavedRecording,
) -> TurnDecision:
    """Shared hold/merge rule (riva-b9r): wait while the user speaks, else merge held + new."""
    if sessions.newer_speech(identity, segment_end_ms):
        sessions.hold(identity, [text])
        write_event(
            Event(EventName.RESPONSE_HELD, identity.session_id, saved.metadata.turn_id),
            sys.stdout,
        )
        return TurnDecision(hold=True, merged=merge_transcripts(sessions.held_preview(identity)))
    previous = sessions.take_held(identity)
    return TurnDecision(
        hold=False, merged=merge_transcripts([*previous, text]), previous=tuple(previous)
    )


def vad_segment_updates(
    payload: str,
    identity: SessionIdentity,
    sessions: "AsrSessions",
    replies: ReplyManager | None,
    *,
    allow_synthetic: bool = False,
) -> Iterator[tuple[Any, ...]]:
    """Developer-UI flow: persist and transcribe a VAD segment, then optionally LLM/TTS reply."""
    timeline = SegmentTimeline(sessions, identity)
    record = timeline.record
    from gradio import skip

    sent_to_llm: str | None = None  # shown in the UI only; never logged

    def update(
        partial: str = "",
        final: str = "",
        status: str = "",
        metadata: dict[str, Any] | None = None,
        reply: str = "",
        metrics: dict[str, Any] | None = None,
    ) -> tuple[Any, ...]:
        return (
            partial,
            final,
            status,
            metadata or {},
            reply,
            metrics or {},
            str((metrics or {}).get("generation_id", "")),
            list(timeline.rows),
            sent_to_llm if sent_to_llm is not None else skip(),
        )

    segment = process_segment(
        payload, identity, sessions, timeline, allow_synthetic=allow_synthetic
    )
    try:
        while True:
            progress = next(segment)
            yield update(progress.partial, progress.final, progress.status, progress.metadata)
    except StopIteration as stop:
        outcome: SegmentOutcome | None = stop.value
    if outcome is None:
        return

    saved, result, duration = outcome.saved, outcome.result, outcome.duration
    detail = f"VAD segment saved · {duration:.2f} s · ASR {result.status}"
    metadata = {**asdict(saved.metadata), "transcription_status": result.status}
    if outcome.synthetic:
        detail = "SYNTHETIC TEST · " + detail
        metadata["synthetic"] = True
    run = None
    created = False
    decision = TurnDecision(hold=False)
    if replies is not None and result.status == "completed" and result.text:
        decision = gate_transcript(sessions, identity, result.text, outcome.segment_end_ms, saved)
        if decision.hold:
            sent_to_llm = "(wartet auf weitere Sprache) " + decision.merged
            detail += " · wartet auf weitere Sprache"
        else:
            sent_to_llm = decision.merged
            try:
                run, created = replies.start(identity, saved, decision.merged, outcome.started_ms)
            except (ValueError, OSError):
                sessions.hold(identity, [*decision.previous, result.text], front=True)
                run = None
                detail += " · Antwort konnte nicht gestartet werden"
    if decision.hold:
        record("reply.held")
    else:
        record("reply.generation_started" if run else "reply.not_started")
    yield update(result.partial, result.text or "", detail, metadata)
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
        for reply_text, _wav, metrics in run.updates():
            yield update(
                result.partial,
                result.text or "",
                f"VAD segment saved · {duration:.2f} s · reply {metrics['status']}",
                metadata,
                reply_text,
                metrics,
            )
        event_name = {
            "completed": EventName.RESPONSE_COMPLETED,
            "cancelled": EventName.RESPONSE_CANCELLED,
        }.get(run.status, EventName.RESPONSE_FAILED)
        write_event(
            Event(event_name, identity.session_id, saved.metadata.turn_id, run.generation_id),
            sys.stdout,
        )
        heard = run.first_playback_ms is not None or run.playback_signal_received
        if run.status == "cancelled" and not heard:
            # Nobody heard this reply: the next turn must answer these words as well.
            sessions.hold(identity, [*decision.previous, result.text or ""], front=True)
        record(
            {"completed": "reply.completed", "cancelled": "reply.cancelled"}.get(
                run.status, "reply.failed"
            )
        )
        yield update(
            result.partial,
            result.text or "",
            detail + f" · reply {run.status}",
            metadata,
            run.text,
            run.snapshot(),
        )


def build_asr_app(sessions: AsrSessions, replies: ReplyManager | None = None) -> "gr.Blocks":
    import gradio as gr

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
            "Keine Wort-Zeitstempel/SRT, keine automatische Gesprächsantwort."
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


VAD_BROWSER_JS = r"""
async (enabled) => {
  const status = (message) => {
    const node = document.querySelector("#riva-vad-state");
    const target = node && (node.querySelector("p") || node);
    if (target) target.textContent = "Browser VAD: " + message;
  };
  const stage = (name) => {
    window.rivaVad = window.rivaVad || {};
    window.rivaVad.stages = (window.rivaVad.stages || []).concat([name]).slice(-12);
    status(window.rivaVad.stages.join(" → "));
  };
  window.rivaVadStage = stage;
  if (!window.rivaVad) {
    window.rivaVad = {
      instance: null, loading: null, enabled: false, busy: false, pending: [], speechEvents: [],
    };
  }
  window.rivaVad.speechEvents = window.rivaVad.speechEvents || [];
  window.rivaVad.pending = window.rivaVad.pending || [];
  // Tell the backend about confirmed speech start/end (browser clock), via a hidden control.
  const fireSpeech = (kind, at) => {
    const host = document.querySelector("#riva-vad-speech");
    const button = host instanceof HTMLButtonElement ? host : host?.querySelector("button");
    if (!button) return;
    window.rivaVad.speechEvents.push({kind: kind, at: at});
    button.click();
  };
  // Stopp (demo page): throw away the utterance being recorded and pause the microphone. The VAD
  // may still report that segment while it pauses, so everything is ignored until Start again.
  window.rivaVad.discardAndPause = () => {
    const vad = window.rivaVad;
    vad.discard = true;
    vad.pending = [];
    if (vad.speaking) fireSpeech("end", performance.now());
    vad.speaking = false;
    vad.speechStartMs = null;
    vad.enabled = false;
    vad.paused = true;
    try { if (vad.instance) vad.instance.pause(); } catch (_) {}
    stage("stopped");
  };
  if (enabled && window.rivaVad.paused && window.rivaVad.instance) {
    window.rivaVad.discard = false;
    window.rivaVad.paused = false;
    window.rivaVad.enabled = true;
    await window.rivaVad.instance.start();
    stage("listening");
    return;
  }
  if (enabled && window.rivaVad.enabled && window.rivaVad.instance) {
    stage("already listening");
    return;
  }
  if (!enabled) {
    window.rivaVad.enabled = false;
    window.rivaVad.pending = [];
    if (window.rivaVad.instance) window.rivaVad.instance.pause();
    stage("paused");
    return;
  }
  stage("start");
  if (!window.rivaVad.loading) {
    window.rivaVad.loading = new Promise((resolve, reject) => {
      const ortScript = document.createElement("script");
      ortScript.src = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.14.0/dist/ort.js";
      ortScript.onload = () => {
        const vadScript = document.createElement("script");
        vadScript.src = "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.27/dist/bundle.min.js";
        vadScript.onload = () => { stage("vad script loaded"); resolve(); };
        vadScript.onerror = () => reject(new Error("vad_script_blocked"));
        document.head.appendChild(vadScript);
      };
      ortScript.onerror = () => reject(new Error("ort_script_blocked"));
      document.head.appendChild(ortScript);
    });
  }
  try {
    await window.rivaVad.loading;
    stage("scripts ready");
    if (!window.vad || !window.vad.MicVAD) throw new Error("VAD library unavailable");
    if (window.rivaVad.instance) window.rivaVad.instance.destroy();
    const submitNext = () => {
      if (!window.rivaVad.enabled || window.rivaVad.busy || !window.rivaVad.pending.length) return;
      window.rivaVad.busy = true;
      const payload = window.rivaVad.pending.shift();
      payload.events.push({stage: "vad.submit_clicked", at_ms: performance.now()});
      window.rivaVad.activePayload = JSON.stringify(payload);
      const host = document.querySelector("#riva-vad-submit");
      const button = host instanceof HTMLButtonElement ? host : host?.querySelector("button");
      if (button) button.click();
      else {
        window.rivaVad.busy = false;
        stage("submit control not found");
      }
    };
    window.rivaVad.submitNext = submitNext;
    const vadBase = "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.27/dist/";
    const ortBase = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.14.0/dist/";
    // The presenter may set the pause threshold (demo app); other pages have no such route.
    let redemptionMs = 900;
    try {
      const response = await fetch("/riva/settings", {cache: "no-store"});
      if (response.ok) {
        const settings = await response.json();
        if (Number.isFinite(settings.redemption_ms)) {
          redemptionMs = Math.min(3000, Math.max(300, settings.redemption_ms));
        }
      }
    } catch (_) {}
    stage("requesting microphone / loading model (pause " + redemptionMs + " ms)");
    window.rivaVad.instance = await window.vad.MicVAD.new({
      model: "v5",
      baseAssetPath: vadBase,
      onnxWASMBasePath: ortBase,
      redemptionMs: redemptionMs,
      preSpeechPadMs: 300,
      minSpeechMs: 250,
      onSpeechStart: () => {
        if (window.rivaVad.discard) return;
        window.rivaVad.speechStartMs = performance.now();
        window.rivaVad.speaking = true;
        if (window.rivaHoldReplyAudio) window.rivaHoldReplyAudio();
        stage("speech start");
      },
      onSpeechRealStart: () => {
        if (window.rivaVad.discard) return;
        // Confirmed speech (not a misfire): the backend stops/holds replies for it.
        fireSpeech("start", window.rivaVad.speechStartMs || performance.now());
        stage("speech confirmed");
      },
      onSpeechEnd: (audio) => {
        if (window.rivaVad.discard) {
          stage("discarded");
          return;
        }
        window.rivaVad.speaking = false;
        const endedMs = performance.now();
        fireSpeech("end", endedMs);
        if (window.rivaDropReplyAudio) window.rivaDropReplyAudio();
        if (!window.rivaVad.enabled) return;
        if (window.rivaVad.pending.length >= 4) {
          if (window.rivaReport) window.rivaReport("segment", "dropped_busy");
          stage("busy: pending segment limit reached, speech dropped");
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
        window.rivaVad.pending.push({
          pcm16_b64: btoa(binary),
          started_ms: window.rivaVad.speechStartMs || endedMs,
          events: [
            {stage: "vad.speech_start", at_ms: window.rivaVad.speechStartMs || endedMs},
            {stage: "vad.speech_end", at_ms: endedMs},
            {stage: "vad.segment_queued", at_ms: endedMs},
          ],
        });
        window.rivaVad.speechStartMs = null;
        stage("speech end, segment queued");
        submitNext();
      },
      onVADMisfire: () => {
        window.rivaVad.speaking = false;
        if (window.rivaResumeReplyAudio) window.rivaResumeReplyAudio();
        stage("misfire (too short)");
      },
    });
    window.rivaVad.enabled = true;
    await window.rivaVad.instance.start();
    stage("listening");
  } catch (error) {
    window.rivaVad.enabled = false;
    const raw = String((error && error.name) || "Error");
    const name = raw.replace(/[^A-Za-z0-9_]/g, "").slice(0, 40);
    const text = String((error && error.message) || "");
    const msg = text.match(/^(vad_script_blocked|ort_script_blocked)$/);
    stage("FAILED: " + (msg ? msg[0] : name));
    console.warn("RIVA VAD initialization failed", error);
  }
}
"""


def build_vad_app(
    sessions: AsrSessions,
    replies: ReplyManager | None = None,
    *,
    test_mode: bool = False,
) -> "gr.Blocks":
    """VAD-only UI; intentionally contains no native Gradio microphone stream."""
    import gradio as gr

    def vad_segment(payload: str, identity: SessionIdentity) -> Iterator[tuple[Any, ...]]:
        yield from vad_segment_updates(
            payload, identity, sessions, replies, allow_synthetic=test_mode
        )

    def synthetic_pipeline(identity: SessionIdentity) -> Iterator[tuple[Any, ...]]:
        pcm = np.zeros(16_000, dtype="<i2")
        indices = np.arange(len(pcm), dtype=np.float32)
        pcm[:] = np.rint(np.sin(2 * np.pi * 440 * indices / 16_000) * 5000).astype("<i2")
        payload = json.dumps(
            {
                "pcm16_b64": base64.b64encode(pcm.tobytes()).decode("ascii"),
                "started_ms": 0.0,
                "events": [{"stage": "harness.injected", "at_ms": 0.0}],
                "synthetic": True,
            }
        )
        yield from vad_segment_updates(payload, identity, sessions, replies, allow_synthetic=True)

    def vad_speech(speech_payload: str, identity: SessionIdentity) -> None:
        """Browser VAD reported confirmed speech start/end (client clock, ms)."""
        try:
            data = json.loads(speech_payload)
            if not isinstance(data, dict) or set(data) != {"kind", "at"}:
                raise ValueError("invalid_speech_event")
            at = data["at"]
            if isinstance(at, bool) or not isinstance(at, (int, float)):
                raise ValueError("invalid_speech_event")
            sessions.note_speech(identity, str(data["kind"]), float(at))
        except (ValueError, TypeError):
            return
        if data["kind"] == "start" and replies is not None:
            # Speaking over an unfinished reply: stop generating it.
            replies.cancel_generating(identity)

    def release(identity: SessionIdentity) -> None:
        sessions.release(identity)
        if replies is not None:
            replies.release(identity)

    def poll_vad(identity: SessionIdentity) -> tuple[str, str, str, list[dict[str, Any]]]:
        result, duration = sessions.snapshot(identity)
        detail = f"{result.status} · empfangen: {duration:.2f} s"
        if result.error_code:
            detail += f" · {result.error_code}"
        return (
            result.partial,
            result.text or "",
            detail,
            sessions.get_vad_diagnostics(identity),
        )

    app = gr.Blocks(analytics_enabled=False, delete_cache=(300, 3600))
    with app:
        gr.Markdown(
            "# RIVA – Continuous VAD (experimental)\n"
            "This page uses browser VAD as its only microphone owner; the native Gradio "
            "microphone transport is not mounted here. Detected 16 kHz speech segments "
            "(with short VAD context padding) are sent to OpenAI ASR and privately saved. "
            "In this voice app, successful transcripts also generate an OpenAI LLM/TTS reply. "
            "Use the separate manual app on port 7860 for click-to-record mode."
        )
        identity = gr.State(
            value=new_development_session, time_to_live=600, delete_callback=release
        )
        enabled = gr.Checkbox(label="Continuous listening", value=not test_mode)
        gr.Markdown("Browser VAD: waiting for page script…", elem_id="riva-vad-state")
        diagnostics = gr.JSON(label="VAD → ASR event timeline")
        payload = gr.Textbox(visible=False)
        submit = gr.Button("Submit speech segment", visible="hidden", elem_id="riva-vad-submit")
        speech_payload = gr.Textbox(visible=False)
        speech = gr.Button("Speech event", visible="hidden", elem_id="riva-vad-speech")
        partial = gr.Textbox(label="Vorläufige Transkription", interactive=False)
        final = gr.Textbox(label="Endgültige Transkription (letztes Segment)", interactive=False)
        llm_input = gr.Textbox(
            label="An das LLM gesendeter Text (zusammengeführt)",
            interactive=False,
            visible=replies is not None,
        )
        status = gr.Textbox(label="Segmentstatus", interactive=False)
        details = gr.JSON(label="Gespeicherte Segmentdaten")
        reply_text = gr.Textbox(label="KI-Antwort", interactive=False, visible=replies is not None)
        reply_metrics = gr.JSON(label="Antwortstatus", visible=replies is not None)
        # Opaque id the page's Web Audio player reads to fetch /riva/reply_audio/<id>.
        generation = gr.Textbox(visible="hidden", elem_id="riva-generation-id")
        diagnostic_timer = gr.Timer(0.25)
        if test_mode:
            gr.Markdown(
                "**Synthetic test mode:** this uses a generated tone and mocked ASR/LLM/TTS; "
                "it makes no provider calls and does not access the microphone."
            )
            synthetic_test = gr.Button("Run synthetic pipeline test")
        submit.click(
            vad_segment,
            inputs=[payload, identity],
            outputs=[
                partial,
                final,
                status,
                details,
                reply_text,
                reply_metrics,
                generation,
                diagnostics,
                llm_input,
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
        enabled.change(
            None,
            inputs=[enabled],
            outputs=[],
            js=VAD_BROWSER_JS,
            queue=False,
            api_visibility="private",
        )
        speech.click(
            vad_speech,
            inputs=[speech_payload, identity],
            outputs=[],
            js=(
                "(payload, identity) => "
                "[JSON.stringify(window.rivaVad.speechEvents.shift() || {}), identity]"
            ),
            queue=False,
            api_visibility="private",
        )
        if test_mode:
            synthetic_test.click(
                synthetic_pipeline,
                inputs=[identity],
                outputs=[
                    partial,
                    final,
                    status,
                    details,
                    reply_text,
                    reply_metrics,
                    generation,
                    diagnostics,
                    llm_input,
                ],
                concurrency_limit=1,
                api_visibility="private",
            )
        app.load(
            None,
            inputs=[enabled],
            outputs=[],
            js=VAD_BROWSER_JS,
            queue=False,
            api_visibility="private",
        )
        diagnostic_timer.tick(
            poll_vad,
            inputs=[identity],
            outputs=[partial, final, status, diagnostics],
            queue=False,
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
        app_kwargs=asr_app_kwargs(app),
    )
