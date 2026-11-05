"""The participant page in headless Chromium: German, simple, accessible, safe to operate."""

import time
from pathlib import Path
from typing import Any

from conftest import mic_browser  # type: ignore[import-not-found]
from support.audio import clip, compose, silence
from support.fakes import FailingWire, delayed_wire
from support.server import running_demo_app
from support.ui import LISTENING, SlowEcho, SlowTone, is_subsequence, load, providers

from riva.demo.text import ENGLISH_ONLY, words
from riva.experiments.vad_self_test import SyntheticWire


def test_page_is_german_with_only_simple_visible_controls(
    tmp_path: Path, playwright_instance: Any
) -> None:
    with (
        running_demo_app(tmp_path, providers()) as app,
        mic_browser(playwright_instance) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").wait_for()
        assert page.evaluate("document.documentElement.lang") == "de"
        assert page.title() == "Mein Tagebuch"
        visible = page.evaluate("document.body.innerText")
        for expected in ("Mein Tagebuch", "▶ Start", "⏹ Stopp", "Das habe ich gehört"):
            assert expected in visible
        assert not [w for w in words(visible) if w.casefold() in ENGLISH_ONLY], visible
        for forbidden in ("Gradio", "API", "Settings", "Einstellungen", "Submit", "Error"):
            assert forbidden not in visible
        # No technical control is visible to the participant: only the two big buttons, the tabs
        # and the closed technical spoiler.
        buttons = [b for b in page.query_selector_all("button") if b.is_visible()]
        names = sorted(b.inner_text().split("\n")[0].strip() for b in buttons)
        assert "▶ Start" in names and "⏹ Stopp" in names
        assert not [
            n
            for n in names
            if n
            not in {
                "▶ Start",
                "⏹ Stopp",
                "Mein Tagebuch",
                "Das habe ich gehört",
                "Für Vorführende: Technik",
            }
        ], names
        for kind in ("checkbox", "range", "radio", "file"):
            assert not [e for e in page.query_selector_all(f"input[type={kind}]") if e.is_visible()]
        assert (
            page.query_selector("footer") is None or not page.query_selector("footer").is_visible()
        )
        assert page.query_selector("[data-phase='idle']") is not None
        assert "Drücke auf Start." in page.inner_text("#riva-status")


def start_listening(page: Any) -> None:
    page.get_by_role("button", name="▶ Start").click()
    page.wait_for_function(LISTENING, timeout=90_000)


def conversation_wav(tmp_path: Path, tail: float = 14.0) -> Path:
    return compose(tmp_path / "mic.wav", silence(1.0), clip("zoo"), silence(tail))


def test_every_phase_is_shown_in_order_and_the_journal_fills(
    tmp_path: Path, playwright_instance: Any
) -> None:
    with (
        running_demo_app(tmp_path, providers(delayed_wire(1.5))) as app,
        mic_browser(playwright_instance, conversation_wav(tmp_path)) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").click()
        page.wait_for_function(
            "window.__phases.includes('speaking') && window.__phases.at(-1) === 'listening'",
            timeout=90_000,
        )
        phases = page.evaluate("window.__phases")
        wanted = ["idle", "starting", "listening", "hearing", "transcribing", "thinking",
                  "preparing", "speaking", "listening"]  # fmt: skip
        assert is_subsequence(wanted, phases), phases
        assert (
            page.get_by_role("tab", name="Mein Tagebuch").get_attribute("aria-selected") == "true"
        )
        assert page.input_value("#riva-journal textarea") == "Synthetischer Testlauf."
        page.get_by_role("tab", name="Das habe ich gehört").click()
        assert page.input_value("#riva-heard textarea") == "Synthetischer Testlauf."
        assert page.input_value("#riva-reply textarea").endswith("?")


def test_stopp_ends_the_spoken_reply_at_once_and_says_so(
    tmp_path: Path, playwright_instance: Any
) -> None:
    with (
        running_demo_app(tmp_path, providers()) as app,
        mic_browser(playwright_instance, conversation_wav(tmp_path)) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").click()
        page.wait_for_function("window.__phases.includes('speaking')", timeout=90_000)
        page.get_by_role("button", name="⏹ Stopp").click()
        page.wait_for_function("window.__phases.at(-1) === 'stopped'", timeout=10_000)
        assert "Gestoppt." in page.inner_text("#riva-status")
        assert page.evaluate("window.rivaPlayerStats.counts.stopped_by_user") >= 1
        assert not page.evaluate("window.rivaIsPlaying()")
        (session,) = [s for s in app.runtime.sessions.values() if not s.closed]
        deadline = time.monotonic() + 10  # the server handler runs a moment after the click
        while session.last_reply_heard is not False and time.monotonic() < deadline:
            time.sleep(0.05)
        assert session.last_reply_heard is False
        (run,) = app.runtime.replies.runs.values()
        assert run.status == "cancelled"
        assert session.journal.passages[0].text == "Synthetischer Testlauf."  # kept


def test_a_reset_by_the_presenter_ends_the_session_and_the_page_starts_a_new_one(
    tmp_path: Path, playwright_instance: Any
) -> None:
    with (
        running_demo_app(tmp_path, providers()) as app,
        mic_browser(playwright_instance) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").wait_for()
        page.wait_for_function("window.rivaServerPhase !== undefined")
        deadline = time.monotonic() + 10
        while not app.runtime.sessions and time.monotonic() < deadline:
            time.sleep(0.05)
        (first,) = app.runtime.sessions.values()
        assert app.runtime.reset(first.identity.session_id) is True  # explicit presenter reset
        page.wait_for_function(
            "document.getElementById('riva-status')?.dataset.phase === 'closed'", timeout=10_000
        )
        assert "Diese Sitzung ist zu Ende" in page.inner_text("#riva-status")
        # The page reloads itself and gets a brand new session.
        page.wait_for_function(
            "document.getElementById('riva-status')?.dataset.phase === 'idle'", timeout=20_000
        )
        deadline = time.monotonic() + 10
        fresh: list[Any] = []
        while not fresh and time.monotonic() < deadline:
            fresh = [s for s in app.runtime.sessions.values() if not s.closed]
            time.sleep(0.05)
        assert len(fresh) == 1 and fresh[0].identity.session_id != first.identity.session_id
        assert fresh[0].journal.passages == []


def test_a_failing_speech_recognizer_shows_a_friendly_message_and_recovers(
    tmp_path: Path, playwright_instance: Any
) -> None:
    with (
        running_demo_app(tmp_path, providers(FailingWire)) as app,
        mic_browser(playwright_instance, conversation_wav(tmp_path, 12.0)) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").click()
        page.wait_for_function("window.__phases.includes('error')", timeout=90_000)
        text = page.inner_text("body")
        assert "Da ist etwas schiefgelaufen" in text and "sensitive" not in text
        page.wait_for_function("window.__phases.at(-1) === 'listening'", timeout=30_000)


def test_a_failing_language_model_still_keeps_the_participants_words(
    tmp_path: Path, playwright_instance: Any
) -> None:
    class Broken:
        def respond(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("secret provider detail")

    with (
        running_demo_app(tmp_path, providers(llm=Broken())) as app,
        mic_browser(playwright_instance, conversation_wav(tmp_path)) as context,
    ):
        page = load(context, app.url)
        start_listening(page)
        page.wait_for_function(
            "document.querySelector('#riva-journal textarea').value.length > 0", timeout=60_000
        )
        assert page.input_value("#riva-journal textarea") == "Synthetischer Testlauf."
        assert "nicht ganz verstanden" in page.input_value("#riva-reply textarea")
        assert "secret" not in page.inner_text("body")
        (session,) = [s for s in app.runtime.sessions.values() if not s.closed]
        assert session.journal.passages[0].source == "verbatim_fallback"


def test_keyboard_users_can_reach_and_operate_the_buttons_with_visible_focus(
    tmp_path: Path, playwright_instance: Any
) -> None:
    with (
        running_demo_app(tmp_path, providers()) as app,
        mic_browser(playwright_instance) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").wait_for()
        order: list[str] = []
        for _ in range(8):
            page.keyboard.press("Tab")
            order.append(page.evaluate("document.activeElement.innerText || ''").strip())
            if "⏹ Stopp" in order:
                break
        assert "▶ Start" in order and "⏹ Stopp" in order
        assert order.index("▶ Start") < order.index("⏹ Stopp")
        outline = page.evaluate("parseFloat(getComputedStyle(document.activeElement).outlineWidth)")
        assert outline >= 3, "keyboard focus must be clearly visible"
        page.focus("button#riva-start")
        page.keyboard.press("Enter")
        page.wait_for_function(LISTENING, timeout=90_000)


def test_automated_accessibility_check_finds_no_serious_problems(
    tmp_path: Path, playwright_instance: Any
) -> None:
    axe = Path("~/.cache/riva-browser-test-assets/axe-core@4.10.2/axe.min.js").expanduser()
    if not axe.exists():
        import pytest

        pytest.skip("axe-core missing; run: uv run python scripts/fetch_browser_test_assets.py")
    with (
        running_demo_app(tmp_path, providers()) as app,
        mic_browser(playwright_instance) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").wait_for()
        page.add_script_tag(path=str(axe))
        result = page.evaluate(
            "axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa']}})"
        )
        serious = [
            (v["id"], v["impact"], [n["target"] for n in v["nodes"]][:3])
            for v in result["violations"]
            if v["impact"] in ("serious", "critical")
        ]
        assert serious == [], serious


def test_the_introduction_is_visible_first_and_nothing_starts_by_itself(
    tmp_path: Path, playwright_instance: Any
) -> None:
    with (
        running_demo_app(tmp_path, providers()) as app,
        mic_browser(playwright_instance) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").wait_for()
        intro = page.locator("#riva-intro")
        assert intro.is_visible()
        text = intro.inner_text()
        for expected in ("So geht's", "Drücke auf Start.", "Erzähl mir von deinem Tag.", "Stopp",
                         "kein Mensch"):  # fmt: skip
            assert expected in text
        assert intro.get_attribute("aria-labelledby") == "riva-intro-title"
        page.wait_for_timeout(1500)  # give any automatic start a chance to happen
        # No microphone, no audio and no playback before the participant presses Start.
        assert page.evaluate(
            "window.rivaVad === undefined || !(window.rivaVad.stages || []).length"
        )
        assert page.evaluate("window.rivaPlayerStats.ctx") == "none"
        assert page.evaluate("document.querySelectorAll('audio').length") == 0
        assert not app.runtime.replies.runs
        page.get_by_role("button", name="▶ Start").click()
        page.wait_for_function(LISTENING, timeout=90_000)
        assert intro.is_visible()  # stays available as a reminder


def test_a_reload_opens_an_isolated_session_and_closes_the_old_one(
    tmp_path: Path, playwright_instance: Any
) -> None:
    with (
        running_demo_app(tmp_path, providers()) as app,
        mic_browser(playwright_instance) as context,
    ):
        page = load(context, app.url)
        page.wait_for_function("window.rivaSessionId", timeout=20_000)
        (first,) = app.runtime.active()
        assert page.evaluate("window.rivaSessionId") == first.identity.session_id
        page.reload()
        page.wait_for_function("window.rivaSessionId", timeout=20_000)
        # The unload beacon closed the old session; the reloaded page owns a new, empty one.
        deadline = time.monotonic() + 15
        while not first.closed and time.monotonic() < deadline:
            time.sleep(0.1)
        assert first.closed and first.close_reason == "page_unloaded"
        (fresh,) = app.runtime.active()
        assert fresh.identity.session_id != first.identity.session_id
        assert fresh.archive.directory != first.archive.directory
        assert fresh.journal.passages == [] and fresh.verbatim == []
        assert page.evaluate("window.rivaSessionId") == fresh.identity.session_id


def test_choosing_a_voice_plays_a_cached_preview_and_is_used_for_the_next_reply(
    tmp_path: Path, playwright_instance: Any
) -> None:
    from riva.demo.providers import Providers

    voices_used: list[str] = []

    class SpySpeech(SlowTone):
        def speech(self, phrase: str, config: Any) -> Any:
            voices_used.append(config.voice)
            return super().speech(phrase, config)

    spy = Providers(SlowEcho(), SpySpeech(), SyntheticWire)
    wav = compose(tmp_path / "mic.wav", silence(1.0), clip("zoo"), silence(12.0))
    with running_demo_app(tmp_path, spy) as app, mic_browser(playwright_instance, wav) as context:
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").wait_for()
        assert page.get_by_label("Stimme").is_visible()
        previews_before = len(voices_used)
        for _ in range(2):  # the second time must come from the cache
            page.get_by_label("Stimme").click()
            with page.expect_request("**/riva/voice_preview/marin"):
                page.get_by_role("option", name="Marin").click()
            page.get_by_label("Stimme").click()
            page.get_by_role("option", name="Coral").click()
        assert page.evaluate("window.rivaPreviewPlays") >= 3
        page.get_by_label("Stimme").click()
        page.get_by_role("option", name="Marin").click()
        (session,) = app.runtime.active()
        deadline = time.monotonic() + 10
        while session.settings.voice != "marin" and time.monotonic() < deadline:
            time.sleep(0.1)
        assert session.settings.voice == "marin"
        # one synthesis per previewed voice, however often it was chosen
        assert voices_used[previews_before:] == ["marin", "coral"]
        assert app.runtime.voices.generated == 2
        page.get_by_role("button", name="▶ Start").click()
        page.wait_for_function("window.__phases.includes('speaking')", timeout=90_000)
        assert voices_used[-1] == "marin"  # the reply is spoken in the chosen voice


def test_a_dark_system_setting_does_not_change_the_page(
    tmp_path: Path, playwright_instance: Any
) -> None:
    with (
        running_demo_app(tmp_path, providers()) as app,
        mic_browser(playwright_instance, color_scheme="dark") as context,
    ):
        page = context.new_page()
        page.goto(app.url)
        page.get_by_role("button", name="▶ Start").wait_for(timeout=30_000)
        assert "__theme=light" in page.url
        assert page.evaluate("document.body.classList.contains('dark')") is False
        assert page.evaluate(
            "getComputedStyle(document.getElementById('riva-status')).backgroundColor"
        ) == ("rgb(243, 244, 246)")
        presenter = context.new_page()
        presenter.goto(app.url + "/presenter/")
        presenter.get_by_role("button", name="Automatisch prüfen").wait_for(timeout=30_000)
        assert "__theme=light" in presenter.url
