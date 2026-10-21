"""One bounded, cancellable OpenAI transcription connection per recorded turn."""

from __future__ import annotations

import base64
import json
import os
import queue
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt
import soxr  # type: ignore[import-untyped]
from websockets.exceptions import InvalidStatus

from riva.audio.recordings import AudioInput

RATE = 24000


def provider_error_code(event: dict[str, Any]) -> str:
    code = event.get("error", {}).get("code")
    return {
        "invalid_api_key": "authentication_failed",
        "model_not_found": "model_unavailable",
        "insufficient_quota": "quota_exceeded",
        "rate_limit_exceeded": "rate_limited",
    }.get(code, "provider_error")


class Wire(Protocol):
    def send(self, message: str) -> None: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class AsrConfig:
    model: str = "gpt-live-transcribe"
    language: str = "de"
    delay: str = "low"
    max_seconds: float = 120
    final_timeout: float = 30
    idle_timeout: float = 20

    def __post_init__(self) -> None:
        if self.model not in ("gpt-live-transcribe", "gpt-transcribe"):
            raise ValueError("Unsupported transcription model")
        if self.delay not in ("minimal", "low", "medium", "high", "xhigh"):
            raise ValueError("Unsupported transcription delay")
        if any(
            not np.isfinite(v) or v <= 0
            for v in (self.max_seconds, self.final_timeout, self.idle_timeout)
        ):
            raise ValueError("ASR limits must be positive and finite")

    def update_event(self) -> dict[str, Any]:
        transcription: dict[str, Any] = {"model": self.model}
        if self.model == "gpt-live-transcribe":
            transcription.update(languages=[self.language], delay=self.delay)
        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": RATE},
                        "transcription": transcription,
                        "turn_detection": None,
                    }
                },
            },
        }


def connect_openai() -> Wire:
    """The key stays in the backend process; never return headers/errors to the UI."""
    from websockets.sync.client import connect

    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ValueError("missing_api_key")
    return connect(
        "wss://api.openai.com/v1/realtime?intent=transcription",
        additional_headers={"Authorization": f"Bearer {key}"},
        open_timeout=10,
        close_timeout=2,
        max_size=4 * 1024 * 1024,
    )


def mono_float(audio: AudioInput) -> tuple[int, npt.NDArray[np.float32]]:
    rate, samples = audio
    if isinstance(rate, bool) or not isinstance(rate, int) or not 8000 <= rate <= 192000:
        raise ValueError("Invalid sample rate")
    if not isinstance(samples, np.ndarray) or samples.ndim not in (1, 2) or not len(samples):
        raise ValueError("Invalid audio shape")
    if samples.ndim == 2 and samples.shape[1] not in (1, 2):
        raise ValueError("Only mono/stereo supported")
    if samples.dtype == np.int16:
        normalized = samples.astype(np.float32) / 32768
    elif samples.dtype == np.int32:
        normalized = (samples.astype(np.float64) / 2147483648).astype(np.float32)
    elif samples.dtype in (np.float32, np.float64):
        if not np.isfinite(samples).all() or np.abs(samples).max() > 1:
            raise ValueError("Invalid audio range")
        normalized = samples.astype(np.float32)
    else:
        raise ValueError("Unsupported audio type")
    mono = normalized.mean(axis=1) if normalized.ndim == 2 else normalized
    return rate, np.asarray(mono, dtype=np.float32)


class PcmEncoder:
    def __init__(self, source_rate: int) -> None:
        self.resampler = soxr.ResampleStream(source_rate, RATE, 1, dtype="float32")

    def encode(self, samples: npt.NDArray[np.float32], *, last: bool = False) -> bytes:
        resampled = np.asarray(self.resampler.resample_chunk(samples, last=last), dtype=np.float32)
        return np.clip(np.rint(resampled * 32768), -32768, 32767).astype("<i2").tobytes()


@dataclass(frozen=True)
class Transcript:
    status: str
    partial: str = ""
    text: str | None = None
    error_code: str | None = None
    item_id: str | None = None


class LiveTurn:
    """Network I/O is isolated in one thread; Gradio callbacks never wait to connect."""

    def __init__(self, config: AsrConfig, factory: Callable[[], Wire] = connect_openai) -> None:
        self.config = config
        self.factory = factory
        self.lock = threading.RLock()
        self.commands: queue.Queue[npt.NDArray[np.float32] | None] = queue.Queue(maxsize=64)
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.result = Transcript("connecting")
        self.chunks: list[npt.NDArray[np.float32]] = []
        self.source_rate = 0
        self.frames = 0
        self.committed = False
        self.committed_item_id: str | None = None
        self.last_input = time.monotonic()
        self.worker = threading.Thread(target=self._run, daemon=True, name="riva-asr")
        self.worker.start()

    def snapshot(self) -> Transcript:
        with self.lock:
            return self.result

    def fail(self, code: str) -> None:
        with self.lock:
            if self.result.status not in ("completed", "failed"):
                self.result = Transcript("failed", self.result.partial, error_code=code)

    def append(self, audio: AudioInput) -> None:
        try:
            rate, mono = mono_float(audio)
        except ValueError:
            self.fail("invalid_audio")
            self.cancelled.set()
            raise
        with self.lock:
            if self.committed:
                raise ValueError("Turn already committed")
            if self.source_rate and self.source_rate != rate:
                self.fail("sample_rate_changed")
                self.cancelled.set()
                raise ValueError("Sample rate changed")
            if (self.frames + len(mono)) / rate > self.config.max_seconds:
                self.fail("duration_limit")
                self.cancelled.set()
                raise ValueError("Recording duration limit exceeded")
            self.source_rate = rate
            self.chunks.append(mono.copy())
            self.frames += len(mono)
            self.last_input = time.monotonic()
            # Continue preserving input after network errors, but never silently
            # reconnect/rebill or present a partial transcript as final.
            if self.result.status == "failed":
                return
            try:
                self.commands.put_nowait(mono.copy())
            except queue.Full:
                self.fail("backpressure")
                self.cancelled.set()

    def audio(self) -> AudioInput:
        with self.lock:
            if not self.chunks:
                raise ValueError("No audio received")
            return self.source_rate, np.concatenate(self.chunks)

    def finish(self) -> Transcript:
        with self.lock:
            if not self.committed:
                self.committed = True
                if self.frames < self.source_rate / 10 or not self.frames:
                    self.fail("audio_too_short")
                    self.cancelled.set()
                elif self.result.status != "failed":
                    try:
                        self.commands.put_nowait(None)
                    except queue.Full:
                        self.fail("backpressure")
                        self.cancelled.set()
        if not self.done.wait(self.config.final_timeout + 12):
            self.fail("final_timeout")
            self.cancelled.set()
        return self.snapshot()

    def close(self) -> None:
        self.cancelled.set()
        self.worker.join(timeout=3)

    def _event(self, event: dict[str, Any], commit_sent: bool) -> None:
        kind = event.get("type", "")
        with self.lock:
            if self.result.status == "failed":
                return
            if kind in ("error", "conversation.item.input_audio_transcription.failed"):
                self.fail(provider_error_code(event))
            elif kind == "input_audio_buffer.committed":
                item = str(event["item_id"])
                if not commit_sent or (self.result.item_id and self.result.item_id != item):
                    self.fail("unexpected_item")
                else:
                    self.committed_item_id = item
            elif kind == "conversation.item.input_audio_transcription.delta":
                item = str(event["item_id"])
                expected_item = self.committed_item_id or self.result.item_id
                if expected_item and expected_item != item:
                    self.fail("unexpected_item")
                else:
                    self.result = Transcript(
                        self.result.status, self.result.partial + str(event["delta"]), item_id=item
                    )
            elif kind == "conversation.item.input_audio_transcription.completed":
                item = str(event["item_id"])
                if not commit_sent or self.committed_item_id != item:
                    self.fail("unexpected_item")
                else:
                    self.result = Transcript(
                        "completed", self.result.partial, str(event["transcript"]), item_id=item
                    )

    def _run(self) -> None:
        wire: Wire | None = None
        try:
            wire = self.factory()
            wire.send(json.dumps(self.config.update_event()))
            ready_deadline = time.monotonic() + 10
            while not self.cancelled.is_set():
                event = json.loads(wire.recv(timeout=max(0.01, ready_deadline - time.monotonic())))
                if event.get("type") in ("session.updated", "transcription_session.updated"):
                    with self.lock:
                        if self.result.status != "failed":
                            self.result = Transcript("listening")
                    break
                if event.get("type") == "error":
                    code = provider_error_code(event)
                    self.fail("configuration_rejected" if code == "provider_error" else code)
                    return
                if time.monotonic() >= ready_deadline:
                    raise TimeoutError
            encoder: PcmEncoder | None = None
            commit_sent = False
            final_deadline = float("inf")
            while not self.cancelled.is_set():
                for _ in range(16):
                    try:
                        chunk = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    if encoder is None:
                        encoder = PcmEncoder(self.source_rate)
                    pcm = (
                        encoder.encode(np.empty(0, dtype=np.float32), last=True)
                        if chunk is None
                        else encoder.encode(chunk)
                    )
                    if pcm:
                        wire.send(
                            json.dumps(
                                {
                                    "type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(pcm).decode("ascii"),
                                }
                            )
                        )
                    if chunk is None:
                        wire.send(json.dumps({"type": "input_audio_buffer.commit"}))
                        commit_sent = True
                        final_deadline = time.monotonic() + self.config.final_timeout
                        with self.lock:
                            if self.result.status != "failed":
                                self.result = Transcript(
                                    "finalizing", self.result.partial, item_id=self.result.item_id
                                )
                        break
                with suppress(TimeoutError):
                    self._event(json.loads(wire.recv(timeout=0.05)), commit_sent)
                if self.snapshot().status in ("completed", "failed"):
                    return
                if time.monotonic() > final_deadline:
                    self.fail("final_timeout")
                    return
                if (
                    not commit_sent
                    and time.monotonic() - self.last_input > self.config.idle_timeout
                ):
                    self.fail("idle_timeout")
                    return
            self.fail("cancelled")
        except InvalidStatus as error:
            self.fail(
                {401: "authentication_failed", 403: "access_denied", 429: "rate_limited"}.get(
                    error.response.status_code, "connection_failed"
                )
            )
        except Exception:
            # Provider payloads/exception text can contain audio/transcripts or
            # authentication details. Only this fixed code leaves the worker.
            self.fail("connection_failed")
        finally:
            if wire is not None:
                with suppress(Exception):
                    wire.close()
            self.done.set()
