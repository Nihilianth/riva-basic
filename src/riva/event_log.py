"""Small, allowlisted JSON event records without free-text conversation content."""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TextIO


class EventName(StrEnum):
    SESSION_STARTED = "session.started"
    RECORDING_SAVED = "recording.saved"
    STREAM_REPLAY_SKIPPED = "stream.replay_skipped"
    ASR_FINALIZED = "asr.finalized"
    RESPONSE_STARTED = "response.started"
    RESPONSE_COMPLETED = "response.completed"
    RESPONSE_FAILED = "response.failed"
    PLAYBACK_FALLBACK = "playback.fallback"
    PLAYBACK_EVENT = "playback.event"
    PLAYBACK_STARTED = "playback.started"
    PLAYBACK_STOPPED = "playback.stopped"
    PROVIDER_FAILED = "provider.failed"


@dataclass(frozen=True, slots=True)
class Event:
    """Use opaque IDs only; do not put names, transcripts, keys, or error text here."""

    name: EventName
    session_id: str
    turn_id: str | None = None
    generation_id: str | None = None
    duration_ms: float | None = None

    def __post_init__(self) -> None:
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")


def write_event(event: Event, stream: TextIO) -> None:
    """Write one JSON line. The caller controls access and retention of the sink."""
    record = {"timestamp": datetime.now(UTC).isoformat(), **asdict(event)}
    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    stream.flush()
