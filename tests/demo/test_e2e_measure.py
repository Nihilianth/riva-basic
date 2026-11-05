"""The end-to-end measurement code, exercised with fakes (no network, no cost)."""

from support.e2e import measure_one_interaction

from riva.demo.providers import fake_providers


def test_the_measurement_runs_a_full_interaction_with_fake_providers(tmp_path) -> None:
    result = measure_one_interaction(fake_providers(), tmp_path / "archive")
    assert result["asr"]["status"] == "completed" and result["asr"]["total_ms"] >= 0
    assert result["asr"]["audio_sent_ms"] is not None and result["asr"]["completed_ms"] is not None
    assert result["llm"]["source"] == "llm" and result["tts"]["status"] == "completed"
    assert result["tts"]["audio_seconds"] > 0 and result["journal_passages"] == 1
    assert result["verbatim"] == ["Synthetischer Testlauf."]
    assert result["end_of_speech_to_first_audio_ms"] is not None
