"""Bounded text generation and ordered, overlapping phrase-to-speech streaming."""

from __future__ import annotations

import io
import json
import math
import os
import queue
import re
import threading
import time
import wave
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import uuid4

from riva.audio.recordings import SavedRecording, SessionIdentity, private_directory
from riva.audio.transcripts import atomic_private_text

RATE = 24000
INSTRUCTIONS = (
    "Du bist ein KI-Assistent für einen technischen Journaling-Test. Antworte auf Deutsch, "
    "freundlich und einfach, mit höchstens zwei kurzen Sätzen und einer konkreten Rückfrage. "
    "Beziehe dich nur auf das Gesagte. Erfinde keine Erlebnisse oder Gefühle. "
    "Keine Aufzählungen, kein Markdown. Du bist kein Mensch und kein Therapeut."
)


@dataclass(frozen=True)
class ReplyConfig:
    llm_model: str = "gpt-4.1-mini"
    tts_model: str = "gpt-4o-mini-tts"
    voice: str = "coral"
    max_chars: int = 1200
    max_audio_seconds: int = 45
    max_seconds: int = 60

    def __post_init__(self) -> None:
        if self.max_chars <= 0 or self.max_audio_seconds <= 0 or self.max_seconds <= 0:
            raise ValueError("Reply limits must be positive")


@dataclass(frozen=True)
class TextPart:
    kind: str
    text: str


class ReplyProvider(Protocol):
    def text(self, transcript: str, config: ReplyConfig) -> Iterator[TextPart]: ...
    def speech(self, phrase: str, config: ReplyConfig) -> Iterator[bytes]: ...


class OpenAIReplyProvider:
    """Dedicated Speech API adapter; its announced shutdown is tracked in riva-ddc."""

    def text(self, transcript: str, config: ReplyConfig) -> Iterator[TextPart]:
        from openai import OpenAI

        with (
            OpenAI(max_retries=0, timeout=30) as client,
            client.responses.stream(
                model=config.llm_model,
                instructions=INSTRUCTIONS,
                input=transcript,
                max_output_tokens=320,
                store=False,
            ) as stream,
        ):
            completed = False
            for event in stream:
                if event.type == "response.output_text.delta":
                    yield TextPart("delta", event.delta)
                elif event.type == "response.completed":
                    if event.response.status != "completed":
                        raise RuntimeError("Incomplete text response")
                    yield TextPart("completed", event.response.output_text)
                    completed = True
                elif event.type in (
                    "response.failed",
                    "response.incomplete",
                    "error",
                    "response.refusal.delta",
                ):
                    raise RuntimeError("Text generation failed")
            if not completed:
                raise RuntimeError("Missing text completion")

    def speech(self, phrase: str, config: ReplyConfig) -> Iterator[bytes]:
        from openai import OpenAI

        with (
            OpenAI(max_retries=0, timeout=30) as client,
            client.audio.speech.with_streaming_response.create(
                model=config.tts_model,
                voice=config.voice,
                input=phrase,
                instructions="Sprich klares, ruhiges Deutsch. Natürlich und langsam.",
                response_format="pcm",
            ) as response,
        ):
            yield from response.iter_bytes(chunk_size=12000)


class PhraseBuffer:
    def __init__(self, limit: int = 160) -> None:
        self.pending = ""
        self.limit = limit

    def feed(self, text: str) -> list[str]:
        self.pending += text
        phrases = []
        while self.pending:
            boundary = re.search(r"[.!?][\"'»”]*(?=\s)", self.pending)
            if boundary and boundary.end() <= self.limit:
                end = boundary.end()
            elif len(self.pending) >= self.limit:
                end = self.pending.rfind(" ", 0, self.limit) or self.limit
                if end < 1:
                    end = self.limit
            else:
                break
            phrase = self.pending[:end].strip()
            self.pending = self.pending[end:].lstrip()
            if phrase:
                phrases.append(phrase)
        return phrases

    def flush(self) -> list[str]:
        phrase, self.pending = self.pending.strip(), ""
        return [phrase] if phrase else []


def pcm_wav(pcm: bytes) -> bytes:
    if len(pcm) % 2:
        raise ValueError("Incomplete PCM sample")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)
    return output.getvalue()


def close_iterator(iterator: object) -> None:
    close = getattr(iterator, "close", None)
    if callable(close):
        with suppress(Exception):
            close()


class ReplyRun:
    def __init__(
        self,
        saved: SavedRecording,
        transcript: str,
        config: ReplyConfig,
        provider: ReplyProvider,
        client_started_ms: float,
    ) -> None:
        if not transcript.strip() or len(transcript) > 8000:
            raise ValueError("A bounded, nonempty final transcript is required")
        if not math.isfinite(client_started_ms) or client_started_ms < 0:
            raise ValueError("Invalid client clock")
        self.generation_id = uuid4().hex
        self.saved, self.config, self.provider = saved, config, provider
        self.root = private_directory(saved.audio_path.parent / "replies" / self.generation_id)
        self.started = time.monotonic()
        self.client_started_ms = client_started_ms
        self.lock = threading.RLock()
        self.persist_lock = threading.Lock()
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.phrases: queue.Queue[str | None] = queue.Queue(maxsize=4)
        self.audio: queue.Queue[bytes] = queue.Queue(maxsize=8)
        self.text = ""
        self.pcm = bytearray()
        self.requested_phrases: list[str] = []
        self.status = "generating"
        self.error_code: str | None = None
        self.text_completed = False
        self.first_text_ms: float | None = None
        self.first_audio_ms: float | None = None
        self.first_playback_ms: float | None = None
        self.playback_signal_received: bool = False
        self.playback_signal_ms: float | None = None
        self.stopped_ms: float | None = None
        self.playback_duration_ms: float | None = None
        self.producer = threading.Thread(target=self._text, args=(transcript,), daemon=True)
        self.synthesizer = threading.Thread(target=self._speech, daemon=True)
        self.producer.start()
        self.synthesizer.start()

    def _put[T](self, target: queue.Queue[T], value: T) -> bool:
        while not self.cancelled.is_set():
            if time.monotonic() - self.started > self.config.max_seconds:
                self.fail("reply_timeout")
                return False
            try:
                target.put(value, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def fail(self, code: str) -> None:
        with self.lock:
            if self.status == "generating":
                self.status, self.error_code = "failed", code
        self.cancelled.set()

    def _text(self, transcript: str) -> None:
        buffer = PhraseBuffer()
        iterator: Iterator[TextPart] | None = None
        try:
            iterator = self.provider.text(transcript, self.config)
            for part in iterator:
                if time.monotonic() - self.started > self.config.max_seconds:
                    self.fail("reply_timeout")
                if self.cancelled.is_set():
                    return
                if part.kind == "delta":
                    with self.lock:
                        if len(self.text) + len(part.text) > self.config.max_chars:
                            self.fail("reply_text_limit")
                            return
                        self.text += part.text
                        if part.text and self.first_text_ms is None:
                            self.first_text_ms = (time.monotonic() - self.started) * 1000
                    for phrase in buffer.feed(part.text):
                        if not self._put(self.phrases, phrase):
                            return
                elif part.kind == "completed":
                    with self.lock:
                        if part.text != self.text or not self.text.strip():
                            self.fail("text_completion_mismatch")
                            return
                        self.text_completed = True
                    for phrase in buffer.flush():
                        if not self._put(self.phrases, phrase):
                            return
                    self._put(self.phrases, None)
                    return
            self.fail("missing_text_completion")
        except Exception:
            self.fail("llm_failed")
        finally:
            if iterator is not None:
                close_iterator(iterator)

    def _audio_chunk(self, pcm: bytes) -> bool:
        with self.lock:
            if len(self.pcm) + len(pcm) > self.config.max_audio_seconds * RATE * 2:
                self.fail("reply_audio_limit")
                return False
            self.pcm.extend(pcm)
            if self.first_audio_ms is None:
                self.first_audio_ms = (time.monotonic() - self.started) * 1000
        return self._put(self.audio, pcm)

    def _speech(self) -> None:
        try:
            while not self.cancelled.is_set():
                if time.monotonic() - self.started > self.config.max_seconds:
                    self.fail("reply_timeout")
                    return
                try:
                    phrase = self.phrases.get(timeout=0.1)
                except queue.Empty:
                    continue
                if phrase is None:
                    with self.lock:
                        if not self.pcm or not self.text_completed:
                            self.fail("empty_reply_audio")
                        elif self.status == "generating":
                            self.status = "completed"
                    return
                with self.lock:
                    self.requested_phrases.append(phrase)
                iterator = self.provider.speech(phrase, self.config)
                pending = b""
                produced = 0
                try:
                    for fragment in iterator:
                        if self.cancelled.is_set():
                            return
                        pending += fragment
                        while len(pending) >= 12000:
                            if not self._audio_chunk(pending[:12000]):
                                return
                            produced += 12000
                            pending = pending[12000:]
                    if len(pending) % 2:
                        self.fail("invalid_pcm")
                        return
                    if pending:
                        if not self._audio_chunk(pending):
                            return
                        produced += len(pending)
                    if not produced:
                        self.fail("empty_reply_audio")
                        return
                finally:
                    close_iterator(iterator)
        except Exception:
            self.fail("tts_failed")
        finally:
            try:
                self.persist()
            except OSError:
                with self.lock:
                    self.status, self.error_code = "failed", "reply_storage_failed"
            self.done.set()

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "generation_id": self.generation_id,
                "turn_id": self.saved.metadata.turn_id,
                "status": self.status,
                "error_code": self.error_code,
                "first_text_ms": self.first_text_ms,
                "first_server_audio_ms": self.first_audio_ms,
                "first_browser_play_event_ms": self.first_playback_ms,
                "client_request_started_ms": self.client_started_ms,
                "audio_seconds_produced": len(self.pcm) / (RATE * 2),
                "stopped_ms": self.stopped_ms,
                "playback_duration_ms": self.playback_duration_ms,
            }

    def updates(self) -> Iterator[tuple[str, bytes | None, dict[str, object]]]:
        last_text = None
        try:
            while not self.done.is_set() or not self.audio.empty():
                if time.monotonic() - self.started > self.config.max_seconds:
                    self.fail("reply_timeout")
                if self.cancelled.is_set():
                    break
                try:
                    pcm = self.audio.get(timeout=0.05)
                except queue.Empty:
                    pcm = None
                if self.cancelled.is_set():
                    break
                with self.lock:
                    text = self.text
                if pcm is not None or text != last_text:
                    yield text, pcm_wav(pcm) if pcm else None, self.snapshot()
                    last_text = text
            if not self.cancelled.is_set():
                self.done.wait(timeout=2)
            with self.lock:
                text = self.text
            yield text, None, self.snapshot()
        finally:
            if not self.done.is_set():
                self.close()

    def record_playback(self, now_ms: float) -> bool:
        elapsed = now_ms - self.client_started_ms
        if not math.isfinite(elapsed) or not 0 <= elapsed <= 180000:
            return False
        with self.lock:
            if self.first_playback_ms is not None or not self.pcm or self.status == "cancelled":
                return False
            self.first_playback_ms = elapsed
        self.persist()
        return True

    def note_playback_signal(self) -> None:
        with self.lock:
            if self.first_playback_ms is None:
                self.playback_signal_received = True
                self.playback_signal_ms = (time.monotonic() - self.started) * 1000

    def persist(self) -> None:
        with self.persist_lock:
            with self.lock:
                if (
                    self.first_playback_ms is None
                    and self.playback_signal_received
                    and self.pcm
                    and self.playback_signal_ms is not None
                ):
                    self.first_playback_ms = self.playback_signal_ms
                text, pcm = self.text, bytes(self.pcm)
                record = {
                    **self.snapshot(),
                    "text": text,
                    "config": asdict(self.config),
                    "text_completed": self.text_completed,
                    "requested_speech_phrases": list(self.requested_phrases),
                    "audio_scope": "synthesized_not_confirmed_heard",
                    "history_scope": "current_final_transcript_only",
                    "playback_metric": "browser_play_event_not_measured_audibility",
                }
            atomic_private_text(self.root / "reply.txt", text)
            if pcm:
                fd = os.open(self.root / "speech.wav", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(pcm_wav(pcm))
            atomic_private_text(
                self.root / "reply.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n"
            )

    def stop(self, now_ms: float | None = None) -> None:
        with self.lock:
            if self.status in ("generating", "completed"):
                self.status = "cancelled"
            while not self.audio.empty():
                with suppress(queue.Empty):
                    self.audio.get_nowait()
            while not self.phrases.empty():
                with suppress(queue.Empty):
                    self.phrases.get_nowait()
            if now_ms is not None:
                elapsed = now_ms - self.client_started_ms
                if math.isfinite(elapsed) and elapsed >= 0:
                    self.stopped_ms = elapsed
                    if self.first_playback_ms is not None:
                        self.playback_duration_ms = max(0.0, elapsed - self.first_playback_ms)
            elif self.stopped_ms is None:
                server_elapsed = (time.monotonic() - self.started) * 1000
                self.stopped_ms = server_elapsed
                if self.first_playback_ms is not None:
                    self.playback_duration_ms = max(0.0, server_elapsed - self.first_playback_ms)
        self.cancelled.set()
        with suppress(OSError):
            self.persist()
        self.producer.join(timeout=1)
        self.synthesizer.join(timeout=1)

    def close(self) -> None:
        self.stop()


class ReplyManager:
    def __init__(
        self,
        config: ReplyConfig,
        factory: Callable[[], ReplyProvider] = OpenAIReplyProvider,
    ) -> None:
        self.config, self.factory = config, factory
        self.lock = threading.RLock()
        self.runs: dict[str, ReplyRun] = {}

    def start(
        self,
        identity: SessionIdentity,
        saved: SavedRecording,
        transcript: str,
        client_started_ms: float,
    ) -> tuple[ReplyRun, bool]:
        if (identity.user_id, identity.session_id) != (
            saved.metadata.user_id,
            saved.metadata.session_id,
        ):
            raise ValueError("Recording does not belong to this session")
        with self.lock:
            old = self.runs.get(identity.session_id)
            if old is not None:
                if (
                    old.saved.metadata.turn_id == saved.metadata.turn_id
                    and old.status != "cancelled"
                ):
                    return old, False
                old.close()
            run = ReplyRun(saved, transcript, self.config, self.factory(), client_started_ms)
            self.runs[identity.session_id] = run
            return run, True

    def current(self, identity: SessionIdentity) -> ReplyRun | None:
        with self.lock:
            return self.runs.get(identity.session_id)

    def find(self, generation_id: str) -> ReplyRun | None:
        with self.lock:
            for run in self.runs.values():
                if run.generation_id == generation_id:
                    return run
        return None

    def record_browser_playback(self, generation_id: str, now_ms: float) -> bool:
        run = self.find(generation_id)
        if run is None:
            return False
        try:
            return run.record_playback(now_ms)
        except (OSError, ValueError):
            return False

    def note_playback_signal(self, generation_id: str) -> None:
        run = self.find(generation_id)
        if run is not None:
            run.note_playback_signal()

    def stop(self, identity: SessionIdentity, now_ms: float | None = None) -> ReplyRun | None:
        with self.lock:
            run = self.runs.get(identity.session_id)
            if run is not None:
                run.stop(now_ms)
                return run
        return None

    def cancel(self, identity: SessionIdentity, now_ms: float | None = None) -> ReplyRun | None:
        return self.stop(identity, now_ms)

    def release(self, identity: SessionIdentity) -> None:
        with self.lock:
            run = self.runs.pop(identity.session_id, None)
        if run is not None:
            run.close()
