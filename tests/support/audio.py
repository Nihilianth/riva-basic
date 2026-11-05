"""Compose fake-microphone WAV files from the synthetic speech clips plus silence/noise."""

from pathlib import Path

import numpy as np
import soundfile as sf

RATE = 16000
SPEECH_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "speech"


def clip(name: str) -> np.ndarray:
    samples, rate = sf.read(SPEECH_DIR / f"{name}.wav", dtype="float32")
    assert rate == RATE
    return np.asarray(samples)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE), dtype="float32")


def noise(seconds: float, level: float = 0.02, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * RATE)) * level).astype("float32")


def compose(path: Path, *parts: np.ndarray) -> Path:
    """Write mono 16 kHz PCM16 WAV: what Chromium plays as the fake microphone."""
    sf.write(path, np.concatenate(parts), RATE, subtype="PCM_16")
    return path
