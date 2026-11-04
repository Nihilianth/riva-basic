"""Synthetic, no-network providers for the optional browser-VAD pipeline harness."""

from __future__ import annotations

import json
import math
import queue
from collections.abc import Iterator
from typing import Any

import numpy as np

from riva.audio.replies import ReplyConfig, TextPart
from riva.audio.transcription import Wire


class SyntheticWire(Wire):
    """Emit deterministic synthetic ASR protocol events; never contacts OpenAI."""

    def __init__(self) -> None:
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.sent_audio = False
        self.closed = False

    def send(self, message: str) -> None:
        event = json.loads(message)
        if event.get("type") == "session.update":
            self.events.put({"type": "session.updated"})
        elif event.get("type") == "input_audio_buffer.append" and not self.sent_audio:
            self.sent_audio = True
            self.events.put(
                {
                    "type": "conversation.item.input_audio_transcription.delta",
                    "item_id": "synthetic-item",
                    "delta": "Synthetischer Testlauf.",
                }
            )
        elif event.get("type") == "input_audio_buffer.commit":
            self.events.put({"type": "input_audio_buffer.committed", "item_id": "synthetic-item"})
            self.events.put(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "synthetic-item",
                    "transcript": "Synthetischer Testlauf.",
                }
            )

    def recv(self, timeout: float | None = None) -> str:
        try:
            event = self.events.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        return json.dumps(event)

    def close(self) -> None:
        self.closed = True


class SyntheticReplyProvider:
    """Deterministic synthetic German response/TTS provider; no paid API calls."""

    text_reply = "Das war ein synthetischer Test. Die Sprachkette funktioniert."

    def text(self, transcript: str, config: ReplyConfig) -> Iterator[TextPart]:
        yield TextPart("delta", self.text_reply)
        yield TextPart("completed", self.text_reply)

    def speech(self, phrase: str, config: ReplyConfig) -> Iterator[bytes]:
        sample_count = 24_000
        samples = np.arange(sample_count, dtype=np.float32)
        tone = 0.12 * np.sin(2 * math.pi * 440 * samples / 24_000)
        pcm = np.clip(np.rint(tone * 32767), -32768, 32767).astype("<i2").tobytes()
        yield pcm
