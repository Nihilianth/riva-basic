"""PAID, opt-in: one real TypeSafe Jev classification call, to confirm the contract live.

Run only with approval (and TYPESAFE_API_KEY in the environment):

    RIVA_RUN_LIVE_TESTS=1 uv run --env-file .env pytest -m live tests/demo/live/test_jev.py -s

One tiny call on a synthetic German sentence; no participant data. Also prints the measured latency.
"""

import os

import pytest

from riva.demo.classifier import LABELS, JevClassifier

pytestmark = pytest.mark.live


def test_jev_classifies_a_meta_sentence() -> None:
    if not os.environ.get("TYPESAFE_API_KEY", "").strip():
        pytest.skip("TYPESAFE_API_KEY not set")
    sentence = "Warum hast du mich das gefragt? Das gehört nicht zu meinem Tag."
    result = JevClassifier().classify(sentence)
    assert result is not None, "no response from Jev"
    print(
        f"\nJev: label={result.label} probability={result.probability:.3f} "
        f"latency_ms={result.latency_ms:.0f}"
    )
    assert result.label in LABELS
    assert result.label == "meta" and result.probability >= 0.6
