"""Load and validate the synthetic German journaling conversation fixtures."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "demo"
JOURNAL_OPS = {"append", "revise", "none"}
EXPECT_KEYS = {
    "journal_op",
    "uncertain",
    "clarification_needed",
    "passage_contains",
    "passage_forbids",
    "reply_has_question",
    "reply_max_sentences",
    "revise_target_turn",
    "flags",
    "vocabulary_word",
}


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    tags: list[str]
    turns: list[dict[str, Any]]


def validate(doc: dict[str, Any]) -> list[str]:
    """Return a list of problems; an empty list means the fixture is valid."""
    problems: list[str] = []
    if doc.get("synthetic") is not True:
        problems.append("fixture must be explicitly synthetic")
    if doc.get("language") != "de":
        problems.append("language must be 'de'")
    if not isinstance(doc.get("scenario"), str) or not doc["scenario"]:
        problems.append("scenario name missing")
    turns = doc.get("turns")
    if not isinstance(turns, list) or not turns:
        return [*problems, "turns missing"]
    for index, turn in enumerate(turns):
        where = f"turn {index}"
        segments = turn.get("segments")
        if not isinstance(segments, list) or not segments:
            problems.append(f"{where}: segments missing")
            continue
        for segment in segments:
            if not isinstance(segment.get("asr_text"), str) or not segment["asr_text"].strip():
                problems.append(f"{where}: empty asr_text")
            ms = segment.get("audio_ms")
            if isinstance(ms, bool) or not isinstance(ms, int) or not 200 <= ms <= 30_000:
                problems.append(f"{where}: audio_ms must be an int in 200..30000")
        if not isinstance(turn.get("intended"), str):
            problems.append(f"{where}: intended missing")
        expect = turn.get("expect")
        if not isinstance(expect, dict):
            problems.append(f"{where}: expect missing")
            continue
        unknown = set(expect) - EXPECT_KEYS
        if unknown:
            problems.append(f"{where}: unknown expect keys {sorted(unknown)}")
        if expect.get("journal_op") not in JOURNAL_OPS:
            problems.append(f"{where}: journal_op must be one of {sorted(JOURNAL_OPS)}")
    return problems


def load_all() -> list[Scenario]:
    scenarios = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        scenarios.append(
            Scenario(doc["scenario"], doc.get("description", ""), doc.get("tags", []), doc["turns"])
        )
    return scenarios
