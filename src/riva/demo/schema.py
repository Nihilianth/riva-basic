"""The structured result the LLM returns for each turn, and how it is validated.

Hard gates are structural only (a broken result is retried or replaced by a fallback). Anything
about *meaning* - invented content, doubtful corrections - is reported as soft flags for the
presenter and for evals, never as a rejection: legitimate repairs must not be thrown away.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from riva.demo.text import grounding_score, looks_german, words

SCHEMA_VERSION = 1
JOURNAL_OPS = ("append", "revise", "none")
EDIT_KINDS = ("asr_fix", "grammar", "disfluency", "clarity")
CONFIDENCES = ("low", "medium", "high")
FLAGS = ("asr_unclear", "possible_distress", "meta")
MAX_REPLY_CHARS = 500
LOW_GROUNDING = 0.6
_MARKDOWN = re.compile(r"[*_#`>]|^\s*[-•]\s|^\s*\d+[.)]\s", re.MULTILINE)


class TurnResultError(ValueError):
    """A turn result failed a structural gate; ``code`` is a fixed, log-safe identifier."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _nullable(kind: str) -> dict[str, Any]:
    return {"type": [kind, "null"]}


def turn_schema() -> dict[str, Any]:
    """Strict JSON Schema (all keys required, no extras) for OpenAI structured outputs."""
    string = {"type": "string"}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "spoken_reply",
            "journal_op",
            "uncertain",
            "clarification_needed",
            "edits",
            "added_facts",
            "vocabulary",
            "flags",
        ],  # fmt: skip
        "properties": {
            "spoken_reply": string,
            "journal_op": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "text", "target_id", "evidence"],
                "properties": {
                    "type": {"type": "string", "enum": list(JOURNAL_OPS)},
                    "text": string,
                    "target_id": _nullable("string"),
                    "evidence": _nullable("string"),
                },
            },
            "uncertain": {"type": "boolean"},
            "clarification_needed": {"type": "boolean"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["from", "to", "kind", "confidence"],
                    "properties": {
                        "from": string,
                        "to": string,
                        "kind": {"type": "string", "enum": list(EDIT_KINDS)},
                        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                    },
                },
            },
            "added_facts": {"type": "array", "items": string},
            "vocabulary": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["word", "explanation"],
                        "properties": {"word": string, "explanation": string},
                    },
                ]
            },
            "flags": {"type": "array", "items": {"type": "string", "enum": list(FLAGS)}},
        },
    }


@dataclass(frozen=True)
class JournalOp:
    type: Literal["append", "revise", "none"]
    text: str
    target_id: str | None
    evidence: str | None


@dataclass(frozen=True)
class Edit:
    before: str
    after: str
    kind: str
    confidence: str


@dataclass(frozen=True)
class TurnResult:
    spoken_reply: str
    journal_op: JournalOp
    uncertain: bool
    clarification_needed: bool
    edits: tuple[Edit, ...]
    added_facts: tuple[str, ...]
    vocabulary: tuple[str, str] | None
    flags: tuple[str, ...]


@dataclass(frozen=True)
class ValidatedTurn:
    result: TurnResult
    soft_flags: tuple[str, ...] = field(default_factory=tuple)
    grounding: float | None = None

    @property
    def uncertain(self) -> bool:
        return self.result.uncertain or bool(self.soft_flags)


def _fail(code: str) -> TurnResultError:
    return TurnResultError(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise _fail(code)


def _str(value: Any, code: str) -> str:
    _require(isinstance(value, str), code)
    return str(value)


def parse_turn(raw: str | dict[str, Any]) -> TurnResult:
    """Parse and structurally check the JSON; raises ``TurnResultError`` with a fixed code."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        raise _fail("invalid_json") from None
    _require(isinstance(data, dict), "schema_mismatch")
    wanted = set(turn_schema()["required"])
    _require(set(data) == wanted, "schema_mismatch")
    op = data["journal_op"]
    _require(
        isinstance(op, dict) and set(op) == {"type", "text", "target_id", "evidence"},
        "bad_journal_op",
    )
    _require(op["type"] in JOURNAL_OPS, "bad_journal_op")
    for key in ("target_id", "evidence"):
        _require(op[key] is None or isinstance(op[key], str), "bad_journal_op")
    _require(isinstance(data["uncertain"], bool), "schema_mismatch")
    _require(isinstance(data["clarification_needed"], bool), "schema_mismatch")
    edits = []
    _require(isinstance(data["edits"], list), "schema_mismatch")
    for edit in data["edits"]:
        _require(
            isinstance(edit, dict) and set(edit) == {"from", "to", "kind", "confidence"}, "bad_edit"
        )
        _require(edit["kind"] in EDIT_KINDS and edit["confidence"] in CONFIDENCES, "bad_edit")
        edits.append(
            Edit(
                _str(edit["from"], "bad_edit"),
                _str(edit["to"], "bad_edit"),
                edit["kind"],
                edit["confidence"],
            )
        )
    _require(isinstance(data["added_facts"], list), "schema_mismatch")
    facts = tuple(_str(fact, "schema_mismatch") for fact in data["added_facts"])
    vocabulary = data["vocabulary"]
    if vocabulary is not None:
        _require(
            isinstance(vocabulary, dict) and set(vocabulary) == {"word", "explanation"},
            "bad_vocabulary",
        )
        vocabulary = (
            _str(vocabulary["word"], "bad_vocabulary"),
            _str(vocabulary["explanation"], "bad_vocabulary"),
        )
        _require(all(part.strip() for part in vocabulary), "bad_vocabulary")
    _require(isinstance(data["flags"], list) and all(f in FLAGS for f in data["flags"]), "bad_flag")
    return TurnResult(
        spoken_reply=_str(data["spoken_reply"], "empty_reply"),
        journal_op=JournalOp(
            op["type"], _str(op["text"], "bad_journal_op"), op["target_id"], op["evidence"]
        ),
        uncertain=data["uncertain"],
        clarification_needed=data["clarification_needed"],
        edits=tuple(edits),
        added_facts=facts,
        vocabulary=vocabulary,
        flags=tuple(data["flags"]),
    )


def validate_turn(
    result: TurnResult,
    transcript: str,
    *,
    revisable_ids: frozenset[str] = frozenset(),
    meta_hint: bool = False,
) -> ValidatedTurn:
    """Apply the hard structural gates, then collect soft flags (never rejecting on meaning)."""
    reply = result.spoken_reply.strip()
    _require(bool(reply), "empty_reply")
    _require(len(reply) <= MAX_REPLY_CHARS, "reply_too_long")
    _require(not _MARKDOWN.search(reply), "markdown_in_reply")
    _require(looks_german(reply), "reply_not_german")

    op = result.journal_op
    soft: list[str] = []
    grounding: float | None = None
    if op.type in ("append", "revise"):
        passage = op.text.strip()
        _require(bool(passage), "empty_passage")
        _require(not _MARKDOWN.search(passage), "markdown_in_passage")
        _require(looks_german(passage), "passage_not_german")
        # Catches elaboration: a faithful correction is about as long as what was said.
        _require(len(words(passage)) <= 2 * len(words(transcript)) + 12, "passage_too_long")
        grounding = grounding_score(passage, transcript)
        if grounding < LOW_GROUNDING:
            soft.append("low_grounding")
    if op.type == "append":
        _require(op.target_id is None, "bad_journal_op")
    elif op.type == "revise":
        _require(op.target_id in revisable_ids, "bad_revise_target")
        evidence = (op.evidence or "").strip()
        _require(bool(evidence), "missing_evidence")
        _require(
            grounding_score(evidence, transcript) >= LOW_GROUNDING, "evidence_not_in_transcript"
        )
    else:  # none: justified by a clarification, unusable audio, or a meta/conversational turn
        _require(op.text == "" and op.target_id is None, "bad_journal_op")
        # meta_hint: the classifier already judged this a meta turn, so accept "none" even if the
        # model did not also set the flag (avoids a bad_journal_op retry-storm + fallback append).
        _require(
            result.clarification_needed
            or "asr_unclear" in result.flags
            or "meta" in result.flags
            or meta_hint,
            "bad_journal_op",
        )

    if result.added_facts:
        soft.append("added_facts")
    if result.vocabulary is not None and result.vocabulary[0].casefold() not in reply.casefold():
        soft.append("vocabulary_not_spoken")
    return ValidatedTurn(result, tuple(soft), grounding)
