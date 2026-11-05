import json

import pytest
from support.conversations import FIXTURE_DIR, load_all, validate

FILES = sorted(FIXTURE_DIR.glob("*.json"))


def test_fixture_families_from_the_brief_are_covered() -> None:
    tags = {tag for scenario in load_all() for tag in scenario.tags}
    assert {
        "fragments",
        "revise",
        "disfluency",
        "ambiguity",
        "asr-error",
        "safety",
        "education",
    } <= tags
    assert len(FILES) >= 8


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_fixture_is_valid_synthetic_german(path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert validate(doc) == []
    assert doc["scenario"] == path.stem


def test_validator_rejects_broken_fixtures() -> None:
    good = json.loads(FILES[0].read_text(encoding="utf-8"))
    assert validate({**good, "synthetic": False})
    assert validate({**good, "language": "en"})
    assert validate({**good, "turns": []})
    broken = json.loads(json.dumps(good))
    broken["turns"][0]["expect"]["journal_op"] = "delete"
    assert validate(broken)
    broken = json.loads(json.dumps(good))
    broken["turns"][0]["segments"][0]["audio_ms"] = 5
    assert validate(broken)
