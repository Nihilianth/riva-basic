"""PAID, opt-in, needs the user's approval: a two-turn conversation on the real services.

    RIVA_RUN_LIVE_TESTS=1 uv run --env-file .env pytest -m live tests/demo/live/test_e2e_smoke.py -s

Two synthetic clips ("Heute war ich im Zoo." then "Elefanten und einen ganz großen Löwen." as the
answer to the follow-up question). Real realtime ASR, one LLM call per turn and the TTS of
each reply.
Cost is bounded by construction (tests/support/e2e.py: at most 3 ASR sessions, 6 LLM calls, 12 TTS
phrases, 90 s of speech); the expected cost is a few cents. Measures ASR, LLM and TTS first-audio
latency; browser capture and playback are not included. Results go to the private archive
(research-data/demo-1/live-eval), never to Git or the event log.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support.e2e import measure_conversation

from riva.audio.recordings import private_directory
from riva.demo.providers import real_providers

pytestmark = pytest.mark.live


def test_a_real_two_turn_conversation_with_per_stage_latency() -> None:
    assert os.environ.get("OPENAI_API_KEY"), "run with --env-file .env"
    root = private_directory(Path(os.environ.get("RIVA_PRIVATE_DIR", "../private")))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = private_directory(root / "research-data" / "demo-1" / "live-eval")
    result = measure_conversation(real_providers(), root / "research-data" / "demo-1-live-smoke")
    (target / f"e2e-smoke-{stamp}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for turn in result["turns"]:
        print(
            f"\n[{turn['clip']}] clip={turn['clip_ms']} ms | ASR={turn['asr']['total_ms']} ms | "
            f"LLM={turn['llm']['latency_ms']} ms (attempts {turn['llm']['attempts']}) | "
            f"TTS first audio={turn['tts']['first_audio_ms']} ms | "
            f"end of speech -> first audio={turn['end_of_speech_to_first_audio_ms']} ms"
        )
    print(f"\nbudget: {result['budget']}")
    assert result["budget"]["estimated_usd"] < 0.5
    for turn in result["turns"]:
        assert turn["asr"]["status"] == "completed" and turn["asr"]["total_ms"] > 0
        assert turn["llm"]["source"] == "llm"
        assert turn["tts"]["status"] == "completed" and turn["tts"]["audio_seconds"] > 0
    assert len(result["verbatim"]) == 2
