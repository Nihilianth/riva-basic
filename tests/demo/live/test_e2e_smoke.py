"""PAID, opt-in, needs the user's approval: one complete interaction on the real services.

    RIVA_RUN_LIVE_TESTS=1 uv run --env-file .env pytest -m live tests/demo/live/test_e2e_smoke.py -s

Cost: one realtime ASR session for a 1.8 s clip, one LLM call (gpt-4.1-mini, ~1,300 input
tokens) and the TTS of a short reply - together a few cents. The clip is a synthetic sentence.
Measures ASR, LLM and TTS first-audio latency; browser capture and playback are not included.
Results go to the private archive (research-data/demo-1/live-eval), never to Git or the event log.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support.e2e import measure_one_interaction

from riva.audio.recordings import private_directory
from riva.demo.providers import real_providers

pytestmark = pytest.mark.live


def test_one_real_interaction_with_per_stage_latency() -> None:
    assert os.environ.get("OPENAI_API_KEY"), "run with --env-file .env"
    root = private_directory(Path(os.environ.get("RIVA_PRIVATE_DIR", "../private")))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = private_directory(root / "research-data" / "demo-1" / "live-eval")
    result = measure_one_interaction(real_providers(), root / "research-data" / "demo-1-live-smoke")
    (target / f"e2e-smoke-{stamp}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"\nclip={result['clip_ms']} ms | ASR total={result['asr']['total_ms']} ms | "
        f"LLM={result['llm']['latency_ms']} ms | "
        f"TTS first audio={result['tts']['first_audio_ms']} ms | "
        f"end of speech -> first audio={result['end_of_speech_to_first_audio_ms']} ms"
    )
    assert result["asr"]["status"] == "completed" and result["asr"]["total_ms"] > 0
    assert result["llm"]["source"] == "llm"
    assert result["tts"]["status"] == "completed" and result["tts"]["audio_seconds"] > 0
    assert result["journal_passages"] == 1
