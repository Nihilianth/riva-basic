"""One-off: synthesize the German speech clips used by the fake-microphone browser tests.

PAID: makes one OpenAI TTS request per phrase (3 short phrases, a few cents in total). Run it only
with the user's approval:

    uv run --env-file .env python scripts/generate_speech_fixtures.py

It refuses to overwrite existing clips (use --force) so it cannot repeat paid calls by accident.
The clips are synthetic (text-to-speech), 16 kHz mono PCM16 WAV, trimmed, without padding; tests
compose silence around them (tests/support/audio.py). Nothing here uses real speakers' voices.
"""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

from riva.audio.replies import RATE, OpenAIReplyProvider, ReplyConfig

TARGET = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "speech"
PHRASES = {
    "zoo": "Heute war ich im Zoo.",
    "elefanten": "Elefanten und einen ganz großen Löwen.",
    "schwimmbad": (
        "Gestern war ich mit meiner Schwester im Schwimmbad und danach haben wir ein Eis gegessen."
    ),
}


def trim(samples: np.ndarray, threshold: float = 0.01) -> np.ndarray:
    voiced = np.flatnonzero(np.abs(samples) > threshold)
    if not voiced.size:
        raise ValueError("synthesized clip is silent")
    pad = int(0.05 * 16000)
    return samples[max(0, voiced[0] - pad) : voiced[-1] + pad]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--force", action="store_true", help="overwrite existing clips (paid again)"
    )
    args = parser.parse_args()
    existing = [name for name in PHRASES if (TARGET / f"{name}.wav").exists()]
    if existing and not args.force:
        raise SystemExit(f"Clips already exist ({', '.join(existing)}); use --force to pay again.")
    TARGET.mkdir(parents=True, exist_ok=True)
    config, provider, manifest = ReplyConfig(), OpenAIReplyProvider(), {}
    for name, phrase in PHRASES.items():
        pcm = b"".join(provider.speech(phrase, config))
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        samples = trim(soxr.resample(samples, RATE, 16000))
        path = TARGET / f"{name}.wav"
        sf.write(path, samples, 16000, subtype="PCM_16")
        manifest[name] = {
            "text": phrase,
            "seconds": round(len(samples) / 16000, 2),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        print(f"{name}: {manifest[name]['seconds']} s, {manifest[name]['bytes']} bytes")
    (TARGET / "manifest.json").write_text(
        json.dumps(
            {
                "synthetic": True,
                "language": "de",
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "tts_model": config.tts_model,
                "voice": config.voice,
                "clips": manifest,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(f"{len(PHRASES)} paid TTS requests made.")


if __name__ == "__main__":
    main()
