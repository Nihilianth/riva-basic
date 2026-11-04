"""German streaming transcription experiment: native Gradio chunks → OpenAI WS."""

import argparse
import base64
import binascii
import json
import os
import re
import sys
import threading
import time
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
        self.vad_diagnostics: dict[str, list[dict[str, Any]]] = {}

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
        if turn is not None:
            turn.live.close()


def asr_app_kwargs(app: "gr.Blocks") -> dict[str, Any]:
    """Pass at launch: Gradio recreates its FastAPI app when launching."""
    index = next(i for i, fn in app.fns.items() if fn.fn and fn.fn.__name__ == "append")
    return {"middleware": [Middleware(AudioDeliveryMiddleware, fn_index=index)]}


def vad_segment_updates(
    payload: str,
    identity: SessionIdentity,
    sessions: AsrSessions,
    replies: ReplyManager | None,
    *,
    allow_synthetic: bool = False,
) -> Iterator[tuple[Any, ...]]:
    """Persist and transcribe one browser-VAD-owned 16 kHz PCM16 segment."""
    handler_started = time.monotonic()
    timeline: list[dict[str, Any]] = []
    sessions.update_vad_diagnostics(identity, timeline)

    def record(stage: str, source: str = "server", elapsed_ms: float | None = None) -> None:
        timeline.append(
            {
                "stage": stage,
                "source": source,
                "elapsed_ms": round(elapsed_ms, 1)
                if elapsed_ms is not None
                else round((time.monotonic() - handler_started) * 1000, 1),
            }
        )
        sessions.update_vad_diagnostics(identity, timeline)

    def update(
        partial: str = "",
        final: str = "",
        status: str = "",
        metadata: dict[str, Any] | None = None,
        reply: str = "",
        audio: bytes | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> tuple[Any, ...]:
        return (
            partial,
            final,
            status,
            metadata or {},
            reply,
            audio,
            metrics or {},
            "",
            list(timeline),
        )

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
            record(str(browser_event["stage"]), "browser", float(at_ms) - float(started_ms))
        record("segment.event_received")
        record("segment.validated")
        sessions.begin(identity)
        sessions.append((16000, samples), identity)
        sessions.current(identity).stopped = True
        record("asr.turn_accepted")
    except (KeyError, ValueError, OSError, binascii.Error):
        record("segment.rejected")
        yield update(status="VAD segment rejected or could not be processed")
        return

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
            yield update(
                partial=result.partial,
                final=result.text or "",
                status=f"ASR {result.status} · segment {duration:.2f} s",
                metadata=metadata,
            )
        if finished.is_set():
            break
        finished.wait(0.1)
    finish_thread.join(timeout=0)
    if "error" in finish_result:
        record("asr.persist_failed")
        yield update(status="VAD segment could not be persisted")
        return
    saved = finish_result["saved"]
    result, duration = sessions.snapshot(identity)
    record("recording.persisted")
    detail = f"VAD segment saved · {duration:.2f} s · ASR {result.status}"
    metadata = {**asdict(saved.metadata), "transcription_status": result.status}
    if synthetic:
        detail = "SYNTHETIC TEST · " + detail
        metadata["synthetic"] = True
    run = None
    if replies is not None and result.status == "completed" and result.text:
        try:
            run, created = replies.start(identity, saved, result.text, float(started_ms))
        except (ValueError, OSError):
            run = None
            detail += " · Antwort konnte nicht gestartet werden"
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
        for reply_text, wav, metrics in run.updates():
            yield update(
                result.partial,
                result.text or "",
                f"VAD segment saved · {duration:.2f} s · reply {metrics['status']}",
                metadata,
                reply_text,
                wav,
                metrics,
            )
        event_name = (
            EventName.RESPONSE_COMPLETED if run.status == "completed" else EventName.RESPONSE_FAILED
        )
        write_event(
            Event(event_name, identity.session_id, saved.metadata.turn_id, run.generation_id),
            sys.stdout,
        )
        record("reply.completed" if run.status == "completed" else "reply.failed")
        yield update(
            result.partial,
            result.text or "",
            detail + f" · reply {run.status}",
            metadata,
            run.text,
            None,
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
  if (!window.rivaVad) {
    window.rivaVad = {instance: null, loading: null, enabled: false, busy: false, pending: []};
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
    stage("requesting microphone / loading model");
    window.rivaVad.instance = await window.vad.MicVAD.new({
      model: "v5",
      baseAssetPath: vadBase,
      onnxWASMBasePath: ortBase,
      redemptionMs: 900,
      preSpeechPadMs: 300,
      minSpeechMs: 250,
      onSpeechStart: () => {
        window.rivaVad.speechStartMs = performance.now();
        stage("speech start");
      },
      onSpeechEnd: (audio) => {
        if (!window.rivaVad.enabled) return;
        if (window.rivaVad.pending.length >= 2) {
          stage("busy: pending segment limit reached");
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
        const endedMs = performance.now();
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
      onVADMisfire: () => stage("misfire (too short)"),
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
        partial = gr.Textbox(label="Vorläufige Transkription", interactive=False)
        final = gr.Textbox(label="Endgültige Transkription", interactive=False)
        status = gr.Textbox(label="Segmentstatus", interactive=False)
        details = gr.JSON(label="Gespeicherte Segmentdaten")
        reply_text = gr.Textbox(label="KI-Antwort", interactive=False, visible=replies is not None)
        reply_audio = gr.Audio(
            label="KI-Stimme",
            streaming=True,
            autoplay=True,
            interactive=False,
            buttons=[],
            visible=replies is not None,
        )
        reply_metrics = gr.JSON(label="Antwortstatus", visible=replies is not None)
        reply_latency = gr.Markdown(visible=replies is not None)
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
                reply_audio,
                reply_metrics,
                reply_latency,
                diagnostics,
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
                    reply_audio,
                    reply_metrics,
                    reply_latency,
                    diagnostics,
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
