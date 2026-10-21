import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from riva.audio.recordings import RecordingStore, SessionIdentity, new_development_session


def test_pcm_recording_is_saved_with_metadata_and_private_permissions(tmp_path: Path) -> None:
    samples = np.zeros(16000, dtype=np.int16)
    samples[1:4] = [-32768, 123, 32767]
    identity = new_development_session()
    saved = RecordingStore(tmp_path).save((16000, samples), identity)
    restored, sample_rate = sf.read(saved.audio_path, dtype="int16")
    np.testing.assert_array_equal(restored, samples)
    assert sample_rate == 16000
    assert sf.info(saved.audio_path).subtype == "PCM_16"
    metadata = json.loads(saved.metadata_path.read_text())
    assert metadata["session_id"] == identity.session_id
    assert metadata["user_id"] == "local-development"
    assert metadata["frames"] == 16000
    assert metadata["duration_seconds"] == 1
    assert metadata["channels"] == 1
    assert metadata["sha256"] == hashlib.sha256(saved.audio_path.read_bytes()).hexdigest()
    assert metadata["transcription_status"] == "not_requested"
    assert saved.audio_path.stat().st_mode & 0o777 == 0o600
    assert saved.metadata_path.stat().st_mode & 0o777 == 0o600
    assert saved.audio_path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("dtype", [np.float32, np.float64, np.int32])
def test_stereo_formats_are_converted_to_pcm16(tmp_path: Path, dtype: type) -> None:
    samples = np.zeros((8000, 2), dtype=dtype)
    saved = RecordingStore(tmp_path).save((8000, samples), new_development_session())
    assert sf.info(saved.audio_path).channels == 2
    assert saved.metadata.duration_seconds == 1


@pytest.mark.parametrize(
    "sample_rate,samples",
    [
        (0, np.zeros(20, dtype=np.int16)),
        (True, np.zeros(20, dtype=np.int16)),
        (16000, np.zeros(0, dtype=np.int16)),
        (16000, np.zeros((20, 3), dtype=np.int16)),
        (16000, np.zeros((2, 2, 2), dtype=np.int16)),
        (16000, np.zeros(20, dtype=np.uint8)),
        (16000, np.array([float("nan")], dtype=np.float32)),
        (16000, np.array([2.0], dtype=np.float32)),
    ],
)
def test_invalid_audio_is_rejected_before_writing(
    tmp_path: Path,
    sample_rate: int,
    samples: np.ndarray,
) -> None:
    with pytest.raises(ValueError):
        RecordingStore(tmp_path).save((sample_rate, samples), new_development_session())
    assert not list(tmp_path.iterdir())


def test_duration_limit_is_enforced(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duration limit"):
        RecordingStore(tmp_path, max_duration_seconds=1).save(
            (8000, np.zeros(8001, dtype=np.int16)), new_development_session()
        )
    assert not list(tmp_path.iterdir())


def test_each_save_has_a_unique_turn_id(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    identity = new_development_session()
    audio = (8000, np.zeros(8000, dtype=np.int16))
    first = store.save(audio, identity)
    second = store.save(audio, identity)
    assert first.metadata.turn_id != second.metadata.turn_id
    assert first.audio_path.exists() and second.audio_path.exists()


def test_git_storage_is_rejected_even_if_directory_is_ignored(tmp_path: Path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    with pytest.raises(ValueError, match="outside every Git"):
        RecordingStore(tmp_path / "private")


def test_path_traversal_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="identity"):
        SessionIdentity("../escape", "session")


def test_failed_write_leaves_no_partial_recording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(sf, "write", fail)
    identity = new_development_session()
    with pytest.raises(OSError):
        RecordingStore(tmp_path).save((8000, np.zeros(80, dtype=np.int16)), identity)
    session = tmp_path / identity.user_id / identity.session_id
    assert not list(session.iterdir())
