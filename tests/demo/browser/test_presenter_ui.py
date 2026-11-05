"""The presenter page in headless Chromium: checks, settings, sessions, reset, stop."""

import time
from pathlib import Path
from typing import Any

from conftest import mic_browser  # type: ignore[import-not-found]
from support.audio import clip, compose, silence
from support.server import running_demo_app
from support.ui import LISTENING, SlowTone, load

from riva.demo.fakes import EchoTurnLLM, FakeProbe
from riva.demo.providers import Providers
from riva.experiments.vad_self_test import SyntheticReplyProvider, SyntheticWire


def demo(tmp_path: Path, probe: FakeProbe | None = None, seconds: int = 1) -> Any:
    tone = type("Tone", (SlowTone,), {"seconds": seconds})()
    providers = Providers(EchoTurnLLM(), tone, SyntheticWire)
    return running_demo_app(tmp_path, providers, probe=probe or FakeProbe())


def open_presenter(context: Any, app: Any) -> Any:
    page = context.new_page()
    page.goto(app.url + "/presenter/")
    page.get_by_role("button", name="Automatisch prüfen").wait_for(timeout=30_000)
    return page


def test_the_presenter_page_is_german_and_the_participant_page_has_none_of_it(
    tmp_path: Path, playwright_instance: Any
) -> None:
    with demo(tmp_path) as app, mic_browser(playwright_instance) as context:
        page = open_presenter(context, app)
        assert page.evaluate("document.documentElement.lang") == "de"
        text = page.inner_text("body")
        expected_texts = ("Vorführenden-Seite", "Vor dem Start: Prüfung", "Einstellungen",
                          "Sitzungen", "Alles anhalten", "Persona")  # fmt: skip
        for expected in expected_texts:
            assert expected in text
        participant = context.new_page()
        participant.goto(app.url)
        participant.get_by_role("button", name="▶ Start").wait_for()
        body = participant.inner_text("body")
        for forbidden in ("Persona", "Sprachmodell", "Vorführenden", "Alles anhalten", "Prüfung"):
            assert forbidden not in body


def test_preflight_runs_automatic_checks_and_lists_what_needs_a_person(
    tmp_path: Path, playwright_instance: Any
) -> None:
    probe = FakeProbe()
    with demo(tmp_path, probe) as app, mic_browser(playwright_instance) as context:
        page = open_presenter(context, app)
        page.get_by_role("button", name="Automatisch prüfen").click()
        table = page.locator("#pf-table")
        table.get_by_text("Sitzungsordner beschreibbar").wait_for(timeout=20_000)
        content = table.inner_text()
        for expected in ("Läuft automatisch", "Braucht dich (Browser)", "Mikrofon-Erlaubnis",
                         "Ton abspielen", "Spracherkennung erreichbar", "✅"):  # fmt: skip
            assert expected in content
        assert (
            "Kostet Geld" not in content and probe.paid_calls == 0
        )  # paid test never runs by itself


def test_microphone_and_tone_checks_report_in_the_browser(
    tmp_path: Path, playwright_instance: Any
) -> None:
    with demo(tmp_path) as app, mic_browser(playwright_instance) as context:
        page = open_presenter(context, app)
        page.get_by_role("button", name="Mikrofon prüfen").click()
        page.get_by_text("Das Mikrofon ist erlaubt.").wait_for(timeout=10_000)
        page.get_by_role("button", name="Testton abspielen").click()
        page.get_by_text("Der Ton läuft").wait_for(timeout=10_000)
        page.get_by_role("button", name="Ja, Ton gehört").click()
        assert "Der Ton wurde gehört." in page.inner_text("#pf-tone-result")


def test_the_paid_voice_test_needs_an_explicit_confirmation(
    tmp_path: Path, playwright_instance: Any
) -> None:
    probe = FakeProbe()
    with demo(tmp_path, probe) as app, mic_browser(playwright_instance) as context:
        page = open_presenter(context, app)
        page.get_by_role("button", name="Stimmtest (kostet Geld)").click()
        page.get_by_text("Bitte zuerst bestätigen").wait_for(timeout=10_000)
        assert probe.paid_calls == 0
        page.get_by_label("Ja, die Kosten (wenige Cent) sind in Ordnung").check()
        page.get_by_role("button", name="Stimmtest (kostet Geld)").click()
        page.get_by_text("Die Stimme hat gesprochen").wait_for(timeout=10_000)
        assert probe.paid_calls == 1


def test_saved_settings_apply_to_new_participant_sessions(
    tmp_path: Path, playwright_instance: Any
) -> None:
    with demo(tmp_path) as app, mic_browser(playwright_instance) as context:
        page = open_presenter(context, app)
        page.get_by_label("Persona").click()
        page.get_by_role("option", name="Freundlicher Begleiter").click()
        page.get_by_label("Sprachmodell").click()
        page.get_by_role("option", name="gpt-4.1", exact=True).click()
        page.locator("input[type=number]").first.fill("1500")
        page.locator("input[type=number]").first.press("Enter")
        page.get_by_role("button", name="Für neue Sitzungen speichern").click()
        page.get_by_text("Gespeichert für neue Sitzungen").wait_for(timeout=10_000)
        assert (app.runtime.defaults.persona_id, app.runtime.defaults.model) == ("buddy", "gpt-4.1")
        assert app.runtime.pause_ms == 1500
        participant = load(context, app.url)
        participant.wait_for_function("window.rivaSessionId", timeout=20_000)
        (session,) = app.runtime.active()
        assert session.settings.persona_id == "buddy" and session.settings.model == "gpt-4.1"


def test_presenter_reset_ends_the_session_and_the_participant_page_restarts(
    tmp_path: Path, playwright_instance: Any
) -> None:
    with demo(tmp_path) as app, mic_browser(playwright_instance) as context:
        participant = load(context, app.url)
        participant.wait_for_function("window.rivaSessionId", timeout=20_000)
        (first,) = app.runtime.active()
        page = open_presenter(context, app)
        page.wait_for_function(
            "document.querySelector('#sessions').innerText.includes('aktiv')", timeout=15_000
        )
        page.get_by_role("button", name="Gewählte Sitzung beenden (Neustart)").click()
        page.get_by_text("Sitzung beendet").wait_for(timeout=10_000)
        assert first.closed and first.close_reason == "presenter_reset"
        page.wait_for_function(
            "document.querySelector('#sessions').innerText.includes('beendet (presenter_reset)')",
            timeout=15_000,
        )
        participant.wait_for_function(
            "document.getElementById('riva-status')?.dataset.phase === 'idle' && "
            f"window.rivaSessionId && window.rivaSessionId !== '{first.identity.session_id}'",
            timeout=30_000,
        )
        (fresh,) = app.runtime.active()
        assert fresh is not first and fresh.journal.passages == []


def test_alles_anhalten_stops_the_spoken_reply_on_the_participant_page(
    tmp_path: Path, playwright_instance: Any
) -> None:
    wav = compose(tmp_path / "mic.wav", silence(1.0), clip("zoo"), silence(14.0))
    with demo(tmp_path, seconds=5) as app, mic_browser(playwright_instance, wav) as context:
        participant = load(context, app.url)
        participant.get_by_role("button", name="▶ Start").click()
        participant.wait_for_function(LISTENING, timeout=90_000)
        participant.wait_for_function(
            "window.rivaIsPlaying && window.rivaIsPlaying()", timeout=60_000
        )
        page = open_presenter(context, app)
        page.get_by_role("button", name="⏹ Alles anhalten").click()
        page.get_by_text("Sprache angehalten in 1 Sitzung").wait_for(timeout=10_000)
        participant.wait_for_function(
            "(window.rivaPlayerStats.counts.stopped_by_user || 0) >= 1", timeout=10_000
        )
        assert not participant.evaluate("window.rivaIsPlaying()")


def test_the_presenter_page_has_no_serious_accessibility_problems(
    tmp_path: Path, playwright_instance: Any
) -> None:
    import pytest

    axe = Path("~/.cache/riva-browser-test-assets/axe-core@4.10.2/axe.min.js").expanduser()
    if not axe.exists():
        pytest.skip("axe-core missing; run: uv run python scripts/fetch_browser_test_assets.py")
    with demo(tmp_path) as app, mic_browser(playwright_instance) as context:
        page = open_presenter(context, app)
        page.add_script_tag(path=str(axe))
        # Gradio's own empty-table placeholder (".empty") carries a prohibited aria attribute that
        # we cannot change; everything else on the page is checked.
        result = page.evaluate(
            "axe.run({exclude: [['.empty']]}, "
            "{runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa']}})"
        )
        serious = [
            (v["id"], v["impact"], [n["target"] for n in v["nodes"]][:3])
            for v in result["violations"]
            if v["impact"] in ("serious", "critical")
        ]
        assert serious == [], serious
        time.sleep(0)


def test_preparing_all_voices_needs_the_cost_confirmation(
    tmp_path: Path, playwright_instance: Any
) -> None:
    class FastTone(SlowTone):
        def speech(self, phrase: str, config: Any) -> Any:
            yield from SyntheticReplyProvider.speech(self, phrase, config)

    providers = Providers(EchoTurnLLM(), FastTone(), SyntheticWire)
    with (
        running_demo_app(tmp_path, providers, probe=FakeProbe()) as app,
        mic_browser(playwright_instance) as context,
    ):
        page = open_presenter(context, app)
        page.get_by_role("button", name="Alle Stimmen vorbereiten (kostet Geld)").click()
        page.get_by_text("Bitte zuerst bestätigen").first.wait_for(timeout=10_000)
        assert app.runtime.voices.generated == 0
        page.get_by_label("Ja, die Kosten (wenige Cent) sind in Ordnung").check()
        page.get_by_role("button", name="Alle Stimmen vorbereiten (kostet Geld)").click()
        page.get_by_text("10 neu erzeugt, 0 waren schon vorhanden").wait_for(timeout=20_000)
        assert app.runtime.voices.cached_count() == 10
        page.get_by_role("button", name="Automatisch prüfen").click()
        page.locator("#pf-table").get_by_text("Alle 10 Vorschauen sind da.").wait_for(
            timeout=15_000
        )
