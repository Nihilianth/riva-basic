import re

import pytest
from support.conversations import load_all
from support.scripted import llm_text_for, passage_id

from riva.demo.config import load_config
from riva.demo.fakes import EchoTurnLLM, ScriptedTurnLLM, turn_json
from riva.demo.prompts import TurnContext
from riva.demo.turn import FALLBACK_REPLY, TurnProcessor

CONFIG = load_config()


def processor(*replies):
    llm = ScriptedTurnLLM(replies)
    return TurnProcessor(llm, CONFIG), llm


def run(proc, text="Heute war ich im Zoo.", **kwargs):
    return proc.process(
        TurnContext(transcript=text), persona_id="buddy", model="gpt-4.1-mini", **kwargs
    )


def test_a_valid_reply_is_used_on_the_first_attempt() -> None:
    proc, llm = processor(turn_json())
    outcome = run(proc)
    assert (outcome.source, outcome.attempts, outcome.error_codes) == ("llm", 1, ())
    assert outcome.spoken_reply.endswith("?") and outcome.journal_op.type == "append"
    call = llm.calls[0]
    assert call["model"] == "gpt-4.1-mini" and call["timeout"] == CONFIG.llm_timeout_seconds
    assert CONFIG.core_prompt in str(call["instructions"])
    assert CONFIG.persona("buddy").prompt in str(call["instructions"])
    assert '"Heute war ich im Zoo."' in str(call["input"])


@pytest.mark.parametrize(
    "bad,code",
    [
        ("{kaputt", "invalid_json"),
        ("", "invalid_json"),  # refusal / empty output
        (turn_json(flags=["panic"]), "bad_flag"),
        (turn_json(spoken_reply="That is great. What did you see?"), "reply_not_german"),
    ],
)
def test_an_invalid_reply_is_retried_once_with_a_hint_then_accepted(bad, code) -> None:
    proc, llm = processor(bad, turn_json())
    outcome = run(proc)
    assert (outcome.source, outcome.attempts, outcome.error_codes) == ("llm", 2, (code,))
    assert code in str(llm.calls[1]["input"]) and code not in str(llm.calls[0]["input"])


def test_two_invalid_replies_fall_back_to_the_verbatim_transcript() -> None:
    proc, _ = processor("{kaputt", turn_json(flags=["panic"]))
    outcome = run(proc, "Ähm ich war im im Zoo")
    assert outcome.source == "fallback" and outcome.spoken_reply == FALLBACK_REPLY
    assert (
        outcome.journal_op.type == "append" and outcome.journal_op.text == "Ähm ich war im im Zoo"
    )
    assert outcome.error_codes == ("invalid_json", "bad_flag") and outcome.uncertain
    assert outcome.result is None and outcome.flags == ()


def test_provider_failure_falls_back_without_retry_and_without_leaking_the_error() -> None:
    proc, llm = processor(RuntimeError("sk-secret prompt body"), turn_json())
    outcome = run(proc)
    assert outcome.source == "fallback" and outcome.attempts == 1
    assert outcome.error_codes == ("llm_failed",) and len(llm.calls) == 1
    assert "secret" not in repr(outcome)


def test_soft_flags_make_the_outcome_uncertain_but_keep_it() -> None:
    invented = turn_json(
        journal_op={
            "type": "append",
            "text": "Ich habe Pinguine und Zebras gesehen.",
            "target_id": None,
            "evidence": None,
        },
        added_facts=["Pinguine"],
    )
    outcome = run(processor(invented)[0])
    assert outcome.source == "llm" and outcome.uncertain
    assert {"added_facts", "low_grounding"} <= set(outcome.validated.soft_flags)


def test_revise_is_limited_to_the_ids_given_to_the_processor() -> None:
    revise = turn_json(
        journal_op={
            "type": "revise",
            "text": "Ich habe drei Elefanten gesehen.",
            "target_id": "p1",
            "evidence": "es waren drei Elefanten",
        }
    )
    text = "Nein, es waren drei Elefanten."
    ok = run(processor(revise)[0], text, revisable_ids=frozenset({"p1"}))
    assert ok.journal_op.type == "revise" and ok.attempts == 1
    out_of_window = run(processor(revise, revise)[0], text, revisable_ids=frozenset({"p2"}))
    assert out_of_window.source == "fallback"
    assert out_of_window.error_codes == ("bad_revise_target", "bad_revise_target")


def test_token_usage_is_summed_over_attempts_for_cost_estimates() -> None:
    proc, _ = processor("{kaputt", turn_json())
    outcome = run(proc)
    assert outcome.input_tokens > 0 and outcome.output_tokens > 0 and outcome.attempts == 2


def test_the_offline_echo_llm_produces_a_valid_turn() -> None:
    outcome = run(TurnProcessor(EchoTurnLLM(), CONFIG), 'Er sagte "Hallo".\nZweite Zeile')
    assert outcome.source == "llm" and outcome.journal_op.text == 'Er sagte "Hallo".\nZweite Zeile'


SCENARIO_TURNS = [
    pytest.param(s.name, i, turn, id=f"{s.name}-{i}")
    for s in load_all()
    for i, turn in enumerate(s.turns)
]


@pytest.mark.parametrize("name,index,turn", SCENARIO_TURNS)
def test_every_fixture_turn_is_accepted_and_meets_its_expectations(name, index, turn) -> None:
    expect = turn["expect"]
    revisable = frozenset(passage_id(i) for i in range(index))
    spoken = " ".join(segment["asr_text"] for segment in turn["segments"])
    proc, _ = processor(llm_text_for(turn, index))
    outcome = run(proc, spoken, revisable_ids=revisable)

    assert outcome.source == "llm", outcome.error_codes
    assert outcome.journal_op.type == expect["journal_op"]
    passage = outcome.journal_op.text
    for word in expect.get("passage_contains", []):
        assert word in passage
    for word in expect.get("passage_forbids", []):
        assert word not in passage
    if expect.get("uncertain"):
        assert outcome.uncertain
    assert set(expect.get("flags", [])) <= set(outcome.flags)
    if expect.get("reply_has_question"):
        assert "?" in outcome.spoken_reply
    if limit := expect.get("reply_max_sentences"):
        assert len(re.findall(r"[.!?]+(?:\s|$)", outcome.spoken_reply)) <= limit
