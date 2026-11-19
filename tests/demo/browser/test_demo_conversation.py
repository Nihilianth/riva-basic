"""Whole journaling conversations in the demo app.

Real VAD fed by the fake microphone; ASR, LLM and TTS are deterministic fakes.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import mic_browser  # type: ignore[import-not-found]
from support.audio import clip, compose, noise, silence
from support.fakes import delayed_wire
from support.server import running_demo_app
from support.ui import LISTENING, RecordingEcho, demo_providers, load


def open_session(app: Any) -> Any:
    (session,) = [s for s in app.runtime.sessions.values() if not s.closed]
    return session


def test_two_utterances_build_one_journal_with_context_for_the_second_turn(
    tmp_path: Path, playwright_instance: Any
) -> None:
    llm = RecordingEcho()
    wav = compose(
        tmp_path / "mic.wav",
        silence(1.0), clip("zoo"), silence(9.0),  # reply to the first utterance plays out
        clip("elefanten"), silence(9.0),
    )  # fmt: skip
    with (
        running_demo_app(tmp_path, demo_providers(llm)) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").click()
        page.wait_for_function(LISTENING, timeout=90_000)
        page.wait_for_function(
            "document.querySelector('#riva-journal textarea').value.split('\\n\\n').length === 2",
            timeout=90_000,
        )
        session = open_session(app)
        assert [p.text for p in session.journal.passages] == ["Synthetischer Testlauf."] * 2
        # The second turn's prompt carries the first passage and the earlier exchange.
        assert len(llm.inputs) == 2
        assert (
            "[p0] Synthetischer Testlauf." in llm.inputs[1]
            and "Bisheriges Gespräch" in llm.inputs[1]
        )
        assert "[p0]" not in llm.inputs[0] and "noch leer" in llm.inputs[0]


def test_a_fragmented_utterance_becomes_one_turn_and_one_journal_entry(
    tmp_path: Path, playwright_instance: Any
) -> None:
    llm = RecordingEcho()
    wav = compose(
        tmp_path / "mic.wav",
        silence(1.0), clip("zoo"), silence(1.8), clip("elefanten"), silence(10.0),
    )  # fmt: skip
    with (
        running_demo_app(tmp_path, demo_providers(llm, delayed_wire(4.0))) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").click()
        page.wait_for_function(
            "document.querySelector('#riva-journal textarea').value.length > 0", timeout=90_000
        )
        assert len(llm.inputs) == 1  # not one LLM call per fragment
        merged = "Synthetischer Testlauf. Synthetischer Testlauf."
        assert json.dumps(merged) in llm.inputs[0]
        session = open_session(app)
        assert (
            len(session.journal.passages) == 1
            and session.verbatim == ["Synthetischer Testlauf."] * 2
        )
        page.get_by_role("tab", name="Das habe ich gehört").click()
        assert page.input_value("#riva-heard textarea") == "\n\n".join(session.verbatim)


@pytest.mark.xfail(
    reason="riva-skm.37: the VAD deaf-while-playing guard suppresses barge-in; this test encodes "
    "the desired behaviour and should pass again once the guard lets corrections through.",
    strict=False,
)
def test_speaking_over_a_reply_stops_it_and_the_next_turn_knows_it_was_not_heard(
    tmp_path: Path, playwright_instance: Any
) -> None:
    llm = RecordingEcho()
    wav = compose(
        tmp_path / "mic.wav",
        silence(1.0), clip("zoo"), silence(3.5),  # the 8 s reply is still playing here
        clip("elefanten"), silence(10.0),
    )  # fmt: skip
    with (
        running_demo_app(tmp_path, demo_providers(llm, seconds=4)) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").click()
        page.wait_for_function(
            "(window.rivaPlayerStats?.counts?.suspended_user_speaking || 0) >= 1", timeout=90_000
        )
        page.wait_for_function("window.rivaVad && window.__phases.includes('hearing')")
        page.wait_for_function(
            "document.querySelector('#riva-journal textarea').value.split('\\n\\n').length === 2",
            timeout=60_000,
        )
        assert len(llm.inputs) == 2 and "nicht ganz gehört" in llm.inputs[1]
        assert "dropped_stale_reply" in page.evaluate("Object.keys(window.rivaPlayerStats.counts)")


def test_background_noise_never_reaches_the_journal(
    tmp_path: Path, playwright_instance: Any
) -> None:
    llm = RecordingEcho()
    wav = compose(
        tmp_path / "mic.wav", silence(1.0), noise(3.0), silence(1.0), clip("zoo"), silence(8.0)
    )
    with (
        running_demo_app(tmp_path, demo_providers(llm)) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").click()
        page.wait_for_function(
            "document.querySelector('#riva-journal textarea').value.length > 0", timeout=90_000
        )
        assert len(llm.inputs) == 1 and len(open_session(app).verbatim) == 1


def test_the_private_archive_holds_the_conversation_and_the_log_stays_content_free(
    tmp_path: Path, playwright_instance: Any, capsys: Any
) -> None:
    llm = RecordingEcho()
    wav = compose(tmp_path / "mic.wav", silence(1.0), clip("zoo"), silence(8.0))
    with (
        running_demo_app(tmp_path, demo_providers(llm)) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").click()
        page.wait_for_function(
            "document.querySelector('#riva-journal textarea').value.length > 0", timeout=90_000
        )
        page.wait_for_function("window.__phases.at(-1) === 'listening'", timeout=30_000)
        session = open_session(app)
        root = session.archive.directory
        events = [json.loads(line) for line in (root / "timeline.jsonl").read_text().splitlines()]
        names = [e["event"] for e in events]
        assert names[0] == "session_started"
        assert {
            "segment_saved",
            "turn_started",
            "llm_response",
            "journal_applied",
            "reply_started",
        } <= set(names)
        assert all("t" in e for e in events)
        (utterance,) = (root / "utterances").glob("*.json")
        record = json.loads(utterance.read_text())
        assert record["verbatim_asr"] == ["Synthetischer Testlauf."] and record["spoken_reply"]
        assert (root / "journal.md").read_text().strip() == "Synthetischer Testlauf."
        saved = next(e for e in events if e["event"] == "segment_saved")
        assert (root / saved["audio"]).is_file() and (root / saved["transcript"]).is_file()
    out = capsys.readouterr().out
    assert "Synthetischer Testlauf" not in out  # transcripts never reach the ordinary log
