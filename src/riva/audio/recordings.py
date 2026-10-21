"""Persist bounded PCM recordings outside Git, without participant-facing auth."""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import numpy.typing as npt
import soundfile as sf  # type: ignore[import-untyped]

type AudioInput = tuple[int, npt.NDArray[Any]]


def private_directory(path: Path) -> Path:
    """Create private directories outside all Git trees, including ignored paths."""
    path = path.expanduser().resolve()
    ancestor = path
    missing = []
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    result = subprocess.run(
        ["git", "-C", str(ancestor), "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode == 0:
        raise ValueError("Private audio storage must be outside every Git working tree")
    if result.returncode != 128:
        raise ValueError("Could not verify private storage location")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise ValueError("Private storage must be a directory")
    return path


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """Development-only identity. Replace with authenticated server identities later."""

    user_id: str
    session_id: str

    def __post_init__(self) -> None:
        for value in (self.user_id, self.session_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
                raise ValueError("Invalid opaque identity")


def new_development_session() -> SessionIdentity:
    return SessionIdentity("local-development", uuid4().hex)


@dataclass(frozen=True, slots=True)
class RecordingMetadata:
    user_id: str
    session_id: str
    turn_id: str
    saved_at: str
    sample_rate_hz: int
    channels: int
    frames: int
    duration_seconds: float
    sha256: str
    format: str = "WAV"
    subtype: str = "PCM_16"
    transcription_status: str = "not_requested"


@dataclass(frozen=True, slots=True)
class SavedRecording:
    audio_path: Path
    metadata_path: Path
    metadata: RecordingMetadata


class RecordingStore:
    def __init__(self, root: Path, *, max_duration_seconds: float = 120) -> None:
        if not np.isfinite(max_duration_seconds) or max_duration_seconds <= 0:
            raise ValueError("Maximum duration must be positive and finite")
        self.root = private_directory(root)
        self.max_duration_seconds = max_duration_seconds

    def save(self, audio: AudioInput, identity: SessionIdentity) -> SavedRecording:
        sample_rate, samples = audio
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
            raise ValueError("Sample rate must be an integer")
        if not 8000 <= sample_rate <= 192000:
            raise ValueError("Unsupported sample rate")
        if not isinstance(samples, np.ndarray) or samples.ndim not in (1, 2):
            raise ValueError("Expected a mono or stereo audio array")
        frames = len(samples)
        channels = 1 if samples.ndim == 1 else samples.shape[1]
        if channels not in (1, 2) or frames == 0:
            raise ValueError("Audio must contain samples in one or two channels")
        duration = frames / sample_rate
        if duration > self.max_duration_seconds:
            raise ValueError("Recording exceeds the configured duration limit")
        if samples.dtype == np.int16:
            pcm = samples
        elif samples.dtype == np.int32:
            pcm = (samples.astype(np.float64) / 2147483648).astype(np.float32)
        elif samples.dtype in (np.float32, np.float64):
            if not np.isfinite(samples).all() or np.abs(samples).max() > 1:
                raise ValueError("Floating-point audio must be finite and in [-1, 1]")
            pcm = samples
        else:
            raise ValueError("Unsupported audio sample type")

        session_root = private_directory(self.root / identity.user_id / identity.session_id)
        turn_id = uuid4().hex
        pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=session_root))
        audio_path = pending / "recording.wav"
        metadata_path = pending / "metadata.json"
        try:
            fd = os.open(audio_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                sf.write(stream, pcm, sample_rate, format="WAV", subtype="PCM_16")
            with audio_path.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            metadata = RecordingMetadata(
                **asdict(identity),
                turn_id=turn_id,
                saved_at=datetime.now(UTC).isoformat(),
                sample_rate_hz=sample_rate,
                channels=channels,
                frames=frames,
                duration_seconds=duration,
                sha256=checksum,
            )
            fd = os.open(metadata_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(asdict(metadata), stream, indent=2)
                stream.write("\n")
            destination = session_root / turn_id
            pending.rename(destination)
        except BaseException:
            # Clean only these two files in the temporary directory created above.
            audio_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            pending.rmdir()
            raise
        return SavedRecording(
            destination / audio_path.name, destination / metadata_path.name, metadata
        )
