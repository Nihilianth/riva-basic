import json
from pathlib import Path


def test_conversation_fixtures_are_explicitly_synthetic() -> None:
    fixtures = list((Path(__file__).parent / "fixtures" / "conversations").glob("*.json"))
    assert fixtures
    for path in fixtures:
        fixture = json.loads(path.read_text())
        assert fixture["synthetic"] is True
        assert fixture["language"] == "de"
        assert fixture["events"]
        assert fixture["expectation"]
