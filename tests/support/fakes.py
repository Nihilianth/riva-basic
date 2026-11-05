"""Deterministic provider fakes with controllable timing, shared by pipeline and browser tests."""

import json
import math
import threading
from collections.abc import Iterator

import numpy as np

from riva.audio.replies import ReplyConfig, TextPart
from riva.experiments.vad_self_test import SyntheticReplyProvider, SyntheticWire


class DelayedWire(SyntheticWire):
    """Synthetic ASR whose final transcript arrives ``delay`` seconds after the commit.

    Real ASR takes ~4-5 s; this lets tests speak again before the previous transcript is final.
    """

    delay = 2.0

    def send(self, message: str) -> None:
        if json.loads(message).get("type") != "input_audio_buffer.commit":
            super().send(message)
            return
        threading.Timer(self.delay, super().send, args=(message,)).start()


def delayed_wire(seconds: float) -> type[DelayedWire]:
    return type("Delayed", (DelayedWire,), {"delay": seconds})


def recording_provider(
    seen: list[str], *, seconds_per_phrase: float = 1.0
) -> type[SyntheticReplyProvider]:
    """Provider factory: records each transcript the "LLM" receives and speaks a longer tone."""

    class Recording(SyntheticReplyProvider):
        def text(self, transcript: str, config: ReplyConfig) -> Iterator[TextPart]:
            seen.append(transcript)
            return super().text(transcript, config)

        def speech(self, phrase: str, config: ReplyConfig) -> Iterator[bytes]:
            count = int(24_000 * seconds_per_phrase)
            tone = 0.12 * np.sin(2 * math.pi * 440 * np.arange(count) / 24_000)
            yield np.clip(np.rint(tone * 32767), -32768, 32767).astype("<i2").tobytes()

    return Recording


class FailingWire(SyntheticWire):
    """ASR that reports a provider error as soon as audio arrives (the message is never shown)."""

    def send(self, message: str) -> None:
        event = json.loads(message)
        if event.get("type") == "input_audio_buffer.append":
            self.events.put({"type": "error", "error": {"message": "sensitive provider text"}})
            return
        super().send(message)
