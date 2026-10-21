"""Private final/failed transcript records, stored next to the received audio."""

import json
import os
import tempfile
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from riva.audio.recordings import SavedRecording
from riva.audio.transcription import AsrConfig, Transcript


def atomic_private_text(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    pending = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def save_transcript(saved: SavedRecording, result: Transcript, config: AsrConfig) -> None:
    if result.status not in ("completed", "failed"):
        raise ValueError("Only terminal ASR results can be persisted")
    root = saved.audio_path.parent
    record = {
        "schema_version": 1,
        "user_id": saved.metadata.user_id,
        "session_id": saved.metadata.session_id,
        "turn_id": saved.metadata.turn_id,
        "saved_at": datetime.now(UTC).isoformat(),
        "provider": "openai",
        "model": config.model,
        "language_hint": config.language if config.model == "gpt-live-transcribe" else None,
        "delay": config.delay if config.model == "gpt-live-transcribe" else None,
        "wire_sample_rate_hz": 24000,
        "audio_scope": "received_gradio_chunks_mono",
        "segments": [],
        "timestamp_source": "unavailable",
        **asdict(result),
    }
    if result.status == "completed":
        atomic_private_text(root / "transcript.txt", (result.text or "") + "\n")
    atomic_private_text(
        root / "transcript.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    )
    metadata = replace(saved.metadata, transcription_status=result.status)
    atomic_private_text(saved.metadata_path, json.dumps(asdict(metadata), indent=2) + "\n")
