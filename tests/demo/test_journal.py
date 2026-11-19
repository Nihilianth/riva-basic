import json

import pytest
from support.conversations import load_all
from support.scripted import llm_text_for

from riva.demo.config import load_config
from riva.demo.fakes import ScriptedTurnLLM, turn_json
from riva.demo.journal import CLARIFY_REPLY, Journal
from riva.demo.prompts import TurnContext
from riva.demo.turn import TurnProcessor

CONFIG = load_config()


def outcome_for(text, journal, llm_json):
    proc = TurnProcessor(ScriptedTurnLLM([llm_json]), CONFIG)
    ctx = TurnContext(transcript=text, journal_tail=journal.tail())
    return proc.process(ctx, persona_id="expert", model="m", revisable_ids=journal.revisable_ids())


def append_json(text, **extra):
    op = {"type": "append", "text": text, "target_id": None, "evidence": None}
    return turn_json(journal_op=op, **extra)


def revise_json(target, text, evidence, **extra):
    op = {"type": "revise", "text": text, "target_id": target, "evidence": evidence}
    return turn_json(journal_op=op, **extra)


def test_the_zoo_example_builds_one_cumulative_entry() -> None:
    journal = Journal()
    for turn_id, (spoken, passage) in enumerate(
        [
            ("Heute war ich im Zoo.", "Heute war ich im Zoo."),
            (
                "Elefanten und einen ganz großen Löwen.",
                "Ich habe Elefanten und einen ganz großen Löwen gesehen.",
            ),
        ]
    ):
        change = journal.apply(outcome_for(spoken, journal, append_json(passage)), f"t{turn_id}")
        assert change.kind == "appended"
    assert journal.text() == (
        "Heute war ich im Zoo.\n\nIch habe Elefanten und einen ganz großen Löwen gesehen."
    )
    assert [p.id for p in journal.passages] == ["p0", "p1"]
    json.dumps(journal.snapshot())


def test_an_explicit_correction_revises_and_keeps_the_old_text() -> None:
    journal = Journal()
    journal.apply(
        outcome_for(
            "Ich habe zwei Elefanten gesehen.",
            journal,
            append_json("Ich habe zwei Elefanten gesehen."),
        ),
        "t0",
    )
    fix = revise_json("p0", "Ich habe drei Elefanten gesehen.", "es waren drei Elefanten")
    change = journal.apply(outcome_for("Nein, es waren drei Elefanten.", journal, fix), "t1")
    assert (change.kind, change.passage_id) == ("revised", "p0")
    assert change.previous_text == "Ich habe zwei Elefanten gesehen."
    passage = journal.passages[0]
    assert passage.text == "Ich habe drei Elefanten gesehen."
    assert [(r.previous_text, r.turn_id, r.reason) for r in passage.revisions] == [
        ("Ich habe zwei Elefanten gesehen.", "t1", "user_correction")
    ]
    assert len(journal.passages) == 1


def test_a_confident_correction_applies_even_when_grounding_is_low() -> None:
    # Regression (riva-skm.32): a real correction restates the entry, so the revised text scores
    # low grounding against the short correction utterance. That soft flag must NOT block the
    # revise when the model is sure (not uncertain, no invented facts) - it used to.
    journal = Journal()
    original = "Ich habe ein paar Fehler in meiner Software behoben. Das hat gut geklappt."
    journal.apply(outcome_for(original, journal, append_json(original)), "t0")
    revised = (
        "Ich habe keine Fehler in meiner Masterarbeit-Software behoben, sondern sie "
        "den ganzen Vormittag nur vorsichtig weiterentwickelt."
    )
    out = outcome_for(
        "Korrektur: ich habe keine Fehler behoben, nur weiterentwickelt.",
        journal,
        revise_json("p0", revised, "keine Fehler behoben, nur weiterentwickelt"),
    )
    assert "low_grounding" in (out.validated.soft_flags if out.validated else ())
    assert out.validated is not None and out.validated.result.uncertain is False
    change = journal.apply(out, "t1")
    assert change.kind == "revised" and journal.passages[0].text == revised


@pytest.mark.parametrize(
    "extra",
    [{"uncertain": True}, {"added_facts": ["vier Löwen"]}],
    ids=["model-uncertain", "invented-fact"],
)
def test_a_doubtful_revise_never_changes_the_journal(extra) -> None:
    journal = Journal()
    original = "Ich habe Elefanten und Löwen gesehen. Danach habe ich ein Eis gegessen."
    journal.apply(outcome_for(original, journal, append_json(original)), "t0")
    guess = revise_json(
        "p0",
        "Ich habe vier Elefanten und Löwen gesehen.",
        "es waren vier",
        spoken_reply="Danke. Du hast also vier Elefanten gesehen.",
        **extra,
    )
    change = journal.apply(outcome_for("Nein, es waren vier.", journal, guess), "t1")
    assert change.kind == "unchanged" and change.reason == "revise_blocked_uncertain"
    assert change.reply_override == CLARIFY_REPLY  # the model's reply had no question
    assert journal.passages[0].text == original and journal.passages[0].revisions == []


def test_a_blocked_revise_keeps_the_models_own_question() -> None:
    journal = Journal()
    journal.apply(outcome_for("Ich sah Löwen.", journal, append_json("Ich sah Löwen.")), "t0")
    guess = revise_json(
        "p0",
        "Ich sah vier Löwen.",
        "es waren vier",
        uncertain=True,
        spoken_reply="Meinst du vier Löwen oder vier Elefanten?",
    )
    change = journal.apply(outcome_for("Nein, es waren vier.", journal, guess), "t1")
    assert change.kind == "unchanged" and change.reply_override is None


def test_none_leaves_the_journal_alone_and_fallback_keeps_the_words_unedited() -> None:
    journal = Journal()
    none = turn_json(
        journal_op={"type": "none", "text": "", "target_id": None, "evidence": None},
        clarification_needed=True,
    )
    assert journal.apply(outcome_for("Mmh.", journal, none), "t0").kind == "unchanged"
    assert journal.passages == []

    broken = TurnProcessor(ScriptedTurnLLM(["{x", "{y"]), CONFIG).process(
        TurnContext(transcript="Ich war im Zoo"), persona_id="expert", model="m"
    )
    change = journal.apply(broken, "t1")
    assert change.reason == "fallback_verbatim"
    assert journal.passages[0].source == "verbatim_fallback" and journal.passages[0].uncertain


def test_only_the_latest_passages_are_visible_and_revisable() -> None:
    journal = Journal(window=2)
    for i in range(4):
        journal.apply(outcome_for(f"Satz {i}.", journal, append_json(f"Satz {i}.")), f"t{i}")
    assert [pid for pid, _ in journal.tail()] == ["p2", "p3"]
    assert journal.revisable_ids() == {"p2", "p3"}
    # An id outside the window is rejected by the validator before it can reach the journal.
    old = revise_json("p0", "Satz 0 neu.", "Satz 0", uncertain=False)
    proc = TurnProcessor(ScriptedTurnLLM([old, old]), CONFIG)
    outcome = proc.process(
        TurnContext(transcript="Nein, Satz 0."),
        persona_id="expert",
        model="m",
        revisable_ids=journal.revisable_ids(),
    )
    assert outcome.source == "fallback" and "bad_revise_target" in outcome.error_codes


@pytest.mark.parametrize("scenario", [s for s in load_all()], ids=lambda s: s.name)
def test_fixture_scenarios_produce_the_expected_journal(scenario) -> None:
    journal = Journal()
    for index, turn in enumerate(scenario.turns):
        spoken = " ".join(segment["asr_text"] for segment in turn["segments"])
        llm = ScriptedTurnLLM([llm_text_for(turn, index)])
        ctx = TurnContext(transcript=spoken, journal_tail=journal.tail())
        outcome = TurnProcessor(llm, CONFIG).process(
            ctx, persona_id="expert", model="m", revisable_ids=journal.revisable_ids()
        )
        change = journal.apply(outcome, f"t{index}")
        expected = turn["expect"]["journal_op"]
        assert {"append": "appended", "revise": "revised", "none": "unchanged"}[expected] == (
            change.kind
            if not turn["expect"].get("uncertain") or expected != "revise"
            else "revised"
        )
    final = journal.text()
    if scenario.name == "self-correction-revise":
        assert "drei" in final and "zwei" not in final
    if scenario.name == "ambiguous-correction":
        assert "Elefanten und Löwen" in final and len(journal.passages) == 1


def test_a_revision_keeps_the_original_text_and_the_utterance_that_caused_it() -> None:
    journal = Journal()
    journal.apply(outcome_for("Ich habe zwei Elefanten gesehen.", journal,
                              append_json("Ich habe zwei Elefanten gesehen.")), "t0", "u0",
                  "Ich habe zwei Elefanten gesehen.")  # fmt: skip
    fix = revise_json("p0", "Ich habe drei Elefanten gesehen.", "es waren drei Elefanten")
    spoken = "Nein, es waren drei Elefanten."
    journal.apply(outcome_for(spoken, journal, fix), "t1", "u1", spoken)
    (revision,) = journal.passages[0].revisions
    assert (revision.previous_text, revision.utterance_id, revision.utterance_text) == (
        "Ich habe zwei Elefanten gesehen.", "u1", spoken)  # fmt: skip
    snapshot = journal.snapshot()[0]["revisions"][0]
    assert snapshot["utterance_text"] == spoken and snapshot["utterance_id"] == "u1"
    assert journal.passages[0].text == "Ich habe drei Elefanten gesehen."


def test_a_blocked_correction_changes_nothing_in_any_passage() -> None:
    journal = Journal()
    for i, text in enumerate(["Ich war im Zoo.", "Ich habe Löwen gesehen."]):
        journal.apply(outcome_for(text, journal, append_json(text)), f"t{i}", f"u{i}", text)
    before = journal.snapshot()
    unsure = revise_json("p1", "Ich habe vier Löwen gesehen.", "es waren vier", uncertain=True)
    change = journal.apply(outcome_for("Nein, es waren vier.", journal, unsure), "t2", "u2", "x")
    assert change.kind == "unchanged" and journal.snapshot() == before
