"""The end-to-end measurement code and its budget guard, tested with fakes (no network, no cost)."""

import pytest
from support.e2e import MAX_LLM_CALLS, Budget, BudgetStop, guarded, measure_conversation

from riva.demo.providers import fake_providers


def test_a_two_turn_conversation_is_measured_per_stage_with_fake_providers(tmp_path) -> None:
    result = measure_conversation(fake_providers(), tmp_path / "archive")
    first, second = result["turns"]
    assert [t["clip"] for t in result["turns"]] == ["zoo", "elefanten"]
    for turn in (first, second):
        assert turn["asr"]["status"] == "completed" and turn["asr"]["total_ms"] >= 0
        assert turn["asr"]["audio_sent_ms"] is not None and turn["asr"]["completed_ms"] is not None
        assert turn["llm"]["source"] == "llm" and turn["tts"]["status"] == "completed"
        assert (
            turn["tts"]["audio_seconds"] > 0 and turn["end_of_speech_to_first_audio_ms"] is not None
        )
    assert result["verbatim"] == ["Synthetischer Testlauf."] * 2
    assert result["journal"].count("Synthetischer Testlauf.") == 2
    assert result["budget"]["asr_sessions"] == 2 and result["budget"]["llm_calls"] == 2
    assert result["budget"]["tts_phrases"] >= 2 and result["budget"]["estimated_usd"] >= 0


def test_the_budget_guard_stops_calls_beyond_its_limits() -> None:
    budget = Budget()
    providers = guarded(fake_providers(), budget)
    for _ in range(MAX_LLM_CALLS):
        providers.llm.respond("i", '"x"', model="m", timeout=1)
    with pytest.raises(BudgetStop):
        providers.llm.respond("i", '"x"', model="m", timeout=1)
    assert budget.llm_calls == MAX_LLM_CALLS
    # the guard is not an Exception, so the turn processor cannot swallow it as a provider error
    assert not issubclass(BudgetStop, Exception)
    assert 0 < budget.worst_case_usd() < 1.0  # what the limits allow stays far below EUR 1
