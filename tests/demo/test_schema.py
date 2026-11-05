import copy
import json

import pytest

from riva.demo.schema import (
    TurnResultError,
    parse_turn,
    turn_schema,
    validate_turn,
)
from riva.demo.text import grounding_score, looks_german, same_word

TRANSCRIPT = "Elefanten und einen ganz großen Löwen."


def good(**overrides):
    base = {
        "spoken_reply": "Das klingt toll. Wie hat der Löwe auf dich gewirkt?",
        "journal_op": {
            "type": "append",
            "text": "Ich habe Elefanten und einen ganz großen Löwen gesehen.",
            "target_id": None,
            "evidence": None,
        },
        "uncertain": False,
        "clarification_needed": False,
        "edits": [
            {
                "from": "Elefanten",
                "to": "Ich habe Elefanten",
                "kind": "grammar",
                "confidence": "high",
            }
        ],
        "added_facts": [],
        "vocabulary": None,
        "flags": [],
    }
    base.update(overrides)
    return base


def check(raw, transcript=TRANSCRIPT, **kwargs):
    return validate_turn(parse_turn(raw), transcript, **kwargs)


def code_of(raw, transcript=TRANSCRIPT, **kwargs):
    with pytest.raises(TurnResultError) as error:
        check(raw, transcript, **kwargs)
    return error.value.code


def test_schema_is_strict_and_matches_the_parser_keys() -> None:
    schema = turn_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"]) == set(good())
    op = schema["properties"]["journal_op"]
    assert set(op["required"]) == set(op["properties"]) and op["additionalProperties"] is False
    json.dumps(schema)  # serializable for the API


def test_a_faithful_turn_passes_without_soft_flags() -> None:
    validated = check(good())
    assert validated.soft_flags == () and not validated.uncertain
    # "gesehen" completes the fragment: a faithful repair still scores well above the soft limit.
    assert validated.grounding is not None and validated.grounding >= 0.7
    assert check(json.dumps(good())).result.spoken_reply.startswith("Das klingt")


@pytest.mark.parametrize(
    "raw,code",
    [
        ("{not json", "invalid_json"),
        ("[]", "schema_mismatch"),
        ({**good(), "extra": 1}, "schema_mismatch"),
        ({k: v for k, v in good().items() if k != "flags"}, "schema_mismatch"),
        (good(flags=["panic"]), "bad_flag"),
        (good(uncertain="no"), "schema_mismatch"),
        (good(edits=[{"from": "a", "to": "b", "kind": "magic", "confidence": "high"}]), "bad_edit"),
        (good(vocabulary={"word": "", "explanation": "x"}), "bad_vocabulary"),
    ],
)
def test_malformed_results_fail_with_fixed_codes(raw, code) -> None:
    assert code_of(raw) == code


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"spoken_reply": "  "}, "empty_reply"),
        ({"spoken_reply": "Wie war " + "das " * 200 + "denn?"}, "reply_too_long"),
        ({"spoken_reply": "**Toll!** Was hast du gesehen?"}, "markdown_in_reply"),
        ({"spoken_reply": "That sounds great, what did you see there?"}, "reply_not_german"),
    ],
)
def test_reply_gates(overrides, code) -> None:
    assert code_of(good(**overrides)) == code


def test_passage_gates_and_soft_signals() -> None:
    def with_text(text, **extra):
        op = {**good()["journal_op"], "text": text}
        return good(journal_op=op, **extra)

    assert code_of(with_text(" ")) == "empty_passage"
    assert code_of(with_text("I saw elephants and a very big lion there.")) == "passage_not_german"
    assert (
        code_of(with_text("Ich habe " + "sehr " * 40 + "viele Tiere gesehen und mehr."))
        == "passage_too_long"
    )
    # Invented content is flagged, not rejected.
    flagged = check(
        with_text("Ich habe Pinguine und Zebras im Zoo gesehen.", added_facts=["Pinguine"])
    )
    assert {"low_grounding", "added_facts"} <= set(flagged.soft_flags) and flagged.uncertain


def test_legitimate_repairs_are_not_rejected() -> None:
    # Number words vs digits, inflection, umlaut folding and filler removal must all stay grounded.
    assert (
        grounding_score("Ich habe 3 Elefanten gesehen.", "Ähm ich habe drei Elefant gesehen") >= 0.9
    )
    assert same_word("Löwen", "Löwe") and same_word("zwei", "2") and same_word("Mähne", "Mahne")
    assert not same_word("Löwe", "Zebra")
    # An ASR homophone repair lowers the score (soft flag), but the turn is still accepted.
    op = {**good()["journal_op"], "text": "Ich habe einen Löwen mit einer großen Mähne gesehen."}
    validated = check(
        good(journal_op=op), "Heute war ich da und habe eine Möwe mit einer großen Mähne gesehen."
    )
    assert validated.soft_flags in ((), ("low_grounding",))


def test_revise_requires_a_listed_target_and_evidence_from_this_utterance() -> None:
    def revise(**fields):
        op = {
            "type": "revise",
            "text": "Ich habe drei Elefanten gesehen.",
            "target_id": "p1",
            "evidence": "es waren drei Elefanten",
            **fields,
        }
        return good(journal_op=op)

    spoken = "Nein, es waren drei Elefanten."
    assert (
        check(revise(), spoken, revisable_ids=frozenset({"p1"})).result.journal_op.type == "revise"
    )
    assert code_of(revise(), spoken, revisable_ids=frozenset({"p2"})) == "bad_revise_target"
    assert (
        code_of(revise(target_id=None), spoken, revisable_ids=frozenset({"p1"}))
        == "bad_revise_target"
    )
    assert (
        code_of(revise(evidence=" "), spoken, revisable_ids=frozenset({"p1"})) == "missing_evidence"
    )
    assert code_of(
        revise(evidence="der Zoo war geschlossen"), spoken, revisable_ids=frozenset({"p1"})
    ) == ("evidence_not_in_transcript")


def test_none_is_only_valid_for_a_clarification_or_unclear_audio() -> None:
    none = {"type": "none", "text": "", "target_id": None, "evidence": None}
    assert check(good(journal_op=none, clarification_needed=True)).result.journal_op.type == "none"
    assert check(good(journal_op=none, flags=["asr_unclear"])).result.journal_op.type == "none"
    assert code_of(good(journal_op=none)) == "bad_journal_op"
    assert (
        code_of(good(journal_op={**none, "text": "Etwas"}, clarification_needed=True))
        == "bad_journal_op"
    )
    assert code_of(good(journal_op={**good()["journal_op"], "target_id": "p1"})) == "bad_journal_op"


def test_vocabulary_must_actually_be_spoken_or_it_is_flagged() -> None:
    spoken = good(
        spoken_reply=(
            "Kennst du das Wort Rüssel? Das ist die lange Nase. Was hat der Elefant gemacht?"
        ),
        vocabulary={"word": "Rüssel", "explanation": "die lange Nase des Elefanten"},
    )
    assert check(spoken).soft_flags == ()
    silent = copy.deepcopy(spoken)
    silent["spoken_reply"] = "Das klingt toll. Was hast du noch gesehen?"
    assert "vocabulary_not_spoken" in check(silent).soft_flags


def test_language_heuristic() -> None:
    assert looks_german("Das war bestimmt schön. Was hast du dort gesehen?")
    assert not looks_german("That was probably nice. What did you see there?")
    assert looks_german("Ja!") and not looks_german("Yes the")
    # Short German passages without any function word must not be rejected.
    assert looks_german("Große graue Elefanten.") and looks_german("Satz 0 neu.")
    assert not looks_german("Big grey elephants are here.")
    # Words that exist in both languages or are German ("also", "was", "in") must not look English.
    assert looks_german("Danke, vier Elefanten also.") and looks_german("Was war in dem Zoo?")
