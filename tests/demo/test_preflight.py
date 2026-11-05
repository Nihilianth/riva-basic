import pytest

from riva.demo.config import load_config
from riva.demo.fakes import FakeProbe
from riva.demo.preflight import (
    KIND_LABELS,
    ProbeError,
    archive_writable,
    as_rows,
    interactive_checks,
    paid_voice_test,
    run_automatic,
)

CONFIG = load_config()


def by_id(checks):
    return {c.id: c for c in checks}


def test_all_automatic_checks_pass_when_services_answer(tmp_path) -> None:
    probe = FakeProbe()
    checks = by_id(run_automatic(CONFIG, probe, tmp_path / "archive"))
    assert {c.status for c in checks.values()} == {"ok"}
    assert {"archive", "api_key", "tts_model", "asr", "voices", "llm_gpt-4.1-mini"} <= set(checks)
    assert probe.paid_calls == 0 and "gpt-4o-mini-tts" in probe.model_lookups
    assert all(c.kind == "automatic" for c in checks.values())


def test_a_missing_key_skips_the_service_probes_with_a_hint(tmp_path) -> None:
    probe = FakeProbe(key=False)
    checks = by_id(run_automatic(CONFIG, probe, tmp_path / "a"))
    assert checks["api_key"].status == "fail" and checks["services"].status == "warn"
    assert probe.model_lookups == []  # nothing is sent without a key


def test_an_unavailable_model_and_an_unreachable_service_are_reported_differently(tmp_path) -> None:
    missing = by_id(run_automatic(CONFIG, FakeProbe(models={"gpt-4.1-mini"}), tmp_path / "a"))
    assert (
        missing["llm_gpt-4.1"].status == "fail"
        and "nicht verfügbar" in missing["llm_gpt-4.1"].detail
    )
    assert missing["tts_model"].status == "fail"
    down = by_id(run_automatic(CONFIG, FakeProbe(down=True), tmp_path / "b"))
    assert down["tts_model"].detail == "Der Dienst ist gerade nicht erreichbar."
    assert down["asr"].status == "fail"


def test_error_text_from_a_provider_never_reaches_the_result(tmp_path) -> None:
    class Leaky(FakeProbe):
        def model_available(self, model):
            raise ProbeError("sk-secret prompt body")

    text = repr(run_automatic(CONFIG, Leaky(), tmp_path / "a"))
    assert "secret" not in text


def test_the_archive_check_writes_a_temp_file_and_refuses_git_trees(tmp_path) -> None:
    ok = archive_writable(tmp_path / "archive")
    assert ok.status == "ok" and list((tmp_path / "archive").iterdir()) == []  # temp file removed
    inside_git = archive_writable(__import__("pathlib").Path(__file__).parent / "never-created")
    assert inside_git.status == "fail" and "Git" in inside_git.detail


def test_interactive_checks_need_a_person_and_are_not_run_automatically() -> None:
    checks = interactive_checks()
    assert {c.id for c in checks} == {"mic", "playback"}
    assert all(c.kind == "interactive" and c.status == "manual" for c in checks)


def test_the_paid_voice_test_refuses_without_confirmation_and_counts_one_call() -> None:
    probe = FakeProbe()
    with pytest.raises(PermissionError):
        paid_voice_test(probe, CONFIG, "coral", confirmed=False)
    assert probe.paid_calls == 0
    result = paid_voice_test(probe, CONFIG, "coral", confirmed=True)
    assert (result.kind, result.status, probe.paid_calls) == ("paid", "ok", 1)
    assert paid_voice_test(probe, CONFIG, "robot", confirmed=True).status == "fail"
    assert probe.paid_calls == 1  # an invalid voice does not spend money


def test_rows_are_german_and_grouped_by_kind(tmp_path) -> None:
    rows = as_rows(run_automatic(CONFIG, FakeProbe(), tmp_path / "a") + interactive_checks())
    assert {row[0] for row in rows} == {KIND_LABELS["automatic"], KIND_LABELS["interactive"]}
    assert all(row[2].startswith(("✅", "⚠️", "❌", "👆")) for row in rows)
