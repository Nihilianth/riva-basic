"""Stopp in every state, with the real VAD and the fake microphone.

Stopp must end the current interaction wherever it is: recording (discard, no submit), ASR and
LLM (late results must not touch the journal or start playback), TTS and playback (no audio, not
even buffered or late audio). Committed journal entries and the audit trail stay. German feedback.
"""

import json
import time
from pathlib import Path
from typing import Any

from conftest import mic_browser  # type: ignore[import-not-found]
from support.audio import clip, compose, silence
from support.fakes import delayed_wire
from support.server import running_demo_app
from support.ui import RecordingEcho, load, slow_tone

from riva.demo.providers import Providers
from riva.experiments.vad_self_test import SyntheticWire


class Echo(RecordingEcho):
    def __init__(self, delay: float = 0.0) -> None:
        super().__init__()
        self.delay = delay

    def respond(self, *args: Any, **kwargs: Any) -> Any:
        time.sleep(self.delay)
        return super().respond(*args, **kwargs)


def setup(llm: Echo, wire: Any = SyntheticWire, tone: Any = None) -> Providers:
    return Providers(llm, tone or slow_tone(0.0, 3), wire)


def start(page: Any) -> None:
    page.get_by_role("button", name="▶ Start").click()


def stopp(page: Any) -> None:
    page.get_by_role("button", name="⏹ Stopp").click()
    page.wait_for_function("window.__phases.at(-1) === 'stopped'", timeout=10_000)
    assert "Gestoppt." in page.inner_text("#riva-status")


def events(session: Any) -> list[dict[str, Any]]:
    path = session.archive.directory / "timeline.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def only_session(app: Any) -> Any:
    (session,) = [s for s in app.runtime.sessions.values() if not s.closed]
    return session


def test_stopp_while_recording_discards_the_utterance_and_submits_nothing(
    tmp_path: Path, playwright_instance: Any
) -> None:
    llm = Echo()
    wav = compose(tmp_path / "mic.wav", silence(1.0), clip("schwimmbad"), silence(8.0))
    with (
        running_demo_app(tmp_path, setup(llm)) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = load(context, app.url)
        start(page)
        page.wait_for_function("window.__phases.includes('hearing')", timeout=90_000)
        stopp(page)
        page.wait_for_timeout(9000)  # the rest of the clip plays into a paused microphone
        session = only_session(app)
        assert llm.inputs == [] and session.verbatim == [] and session.journal.passages == []
        assert app.runtime.asr.turns.get(session.identity.session_id) is None  # nothing submitted
        assert list(session.archive.directory.glob("*/recording.wav")) == []  # nothing was saved
        assert page.evaluate("window.rivaVad.paused") is True
        assert "stopped" in page.evaluate("window.rivaVad.stages")
        assert page.evaluate("window.__phases").count("transcribing") == 0
        assert page.evaluate("document.getElementById('riva-status').dataset.phase") == "stopped"
        assert [e["event"] for e in events(session)].count("interrupted") == 1


def test_stopp_during_asr_discards_the_late_transcript_but_keeps_the_audio(
    tmp_path: Path, playwright_instance: Any
) -> None:
    llm = Echo()
    wav = compose(tmp_path / "mic.wav", silence(1.0), clip("zoo"), silence(12.0))
    with (
        running_demo_app(tmp_path, setup(llm, delayed_wire(4.0))) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = load(context, app.url)
        start(page)
        page.wait_for_function("window.__phases.includes('transcribing')", timeout=90_000)
        stopp(page)
        page.wait_for_timeout(7000)  # the 4 s transcript arrives late
        session = only_session(app)
        phases = page.evaluate("window.__phases")
        assert (
            llm.inputs == [] and session.journal.passages == [] and app.runtime.replies.runs == {}
        )
        assert "thinking" not in phases and "error" not in phases and phases[-1] == "stopped"
        names = [e["event"] for e in events(session)]
        assert "interrupted" in names and "stale_discarded" in names
        assert len(list(session.archive.directory.glob("*/recording.wav"))) == 1  # audit trail


def test_stopp_while_the_model_thinks_drops_its_late_answer(
    tmp_path: Path, playwright_instance: Any
) -> None:
    llm = Echo(delay=4.0)
    wav = compose(tmp_path / "mic.wav", silence(1.0), clip("zoo"), silence(12.0))
    with (
        running_demo_app(tmp_path, setup(llm)) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = load(context, app.url)
        start(page)
        page.wait_for_function("window.__phases.includes('thinking')", timeout=90_000)
        stopp(page)
        page.wait_for_timeout(6000)  # the model answers late
        session = only_session(app)
        phases = page.evaluate("window.__phases")
        assert len(llm.inputs) == 1  # the call was already running; its result is what is dropped
        assert session.journal.passages == [] and app.runtime.replies.runs == {}
        assert not {"preparing", "speaking", "error"} & set(phases) and phases[-1] == "stopped"
        assert page.evaluate("window.rivaPlayerStats.counts.stream_started || 0") == 0
        stale = [e for e in events(session) if e["event"] == "stale_discarded"]
        assert [e["stage"] for e in stale] == ["after_llm"]


def test_stopp_while_the_voice_is_prepared_means_no_audio_ever_starts(
    tmp_path: Path, playwright_instance: Any
) -> None:
    llm = Echo()
    wav = compose(tmp_path / "mic.wav", silence(1.0), clip("zoo"), silence(12.0))
    with (
        running_demo_app(tmp_path, setup(llm, tone=slow_tone(4.0, 3))) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = load(context, app.url)
        start(page)
        page.wait_for_function("window.__phases.includes('preparing')", timeout=90_000)
        stopp(page)
        page.wait_for_timeout(7000)  # the provider delivers its audio late
        session = only_session(app)
        (run,) = app.runtime.replies.runs.values()
        assert run.status == "cancelled" and "speaking" not in page.evaluate("window.__phases")
        assert page.evaluate("window.rivaPlayerStats.chunks") == 0  # no chunk was ever scheduled
        assert session.journal.passages[0].text == "Synthetischer Testlauf."  # committed entry kept


def test_after_a_stopp_start_listens_again_and_the_next_utterance_is_clean(
    tmp_path: Path, playwright_instance: Any
) -> None:
    llm = Echo(delay=3.0)
    wav = compose(
        tmp_path / "mic.wav",
        silence(1.0), clip("zoo"), silence(12.0), clip("elefanten"), silence(10.0),
    )  # fmt: skip
    with (
        running_demo_app(tmp_path, setup(llm)) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = load(context, app.url)
        start(page)
        page.wait_for_function("window.__phases.includes('thinking')", timeout=90_000)
        stopp(page)
        start(page)  # the participant decides to go on
        page.wait_for_function("window.__phases.at(-1) === 'listening'", timeout=15_000)
        page.wait_for_function(
            "document.querySelector('#riva-journal textarea').value.length > 0", timeout=90_000
        )
        session = only_session(app)
        assert len(llm.inputs) == 2
        assert "noch leer" in llm.inputs[1]  # the stopped turn left no trace in the next prompt
        assert len(session.journal.passages) == 1
        assert page.input_value("#riva-journal textarea") == "Synthetischer Testlauf."


def test_the_wait_is_made_visible_and_a_long_wait_is_explained(
    tmp_path: Path, playwright_instance: Any
) -> None:
    llm = Echo(delay=6.0)
    wav = compose(tmp_path / "mic.wav", silence(1.0), clip("zoo"), silence(14.0))
    with (
        running_demo_app(tmp_path, setup(llm)) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = load(context, app.url)
        page.get_by_role("button", name="▶ Start").wait_for()
        assert page.evaluate("document.getElementById('riva-status').dataset.working") == "false"
        start(page)
        page.wait_for_function(
            "document.getElementById('riva-status').dataset.phase === 'thinking'", timeout=90_000
        )
        assert page.evaluate("document.getElementById('riva-status').dataset.working") == "true"
        assert page.locator("#riva-working").is_visible()  # the animated bar
        assert page.inner_text("#riva-status-note").strip() == ""  # not yet: short waits stay quiet
        page.wait_for_function(
            "document.getElementById('riva-status-note').textContent.includes('einen Moment')",
            timeout=15_000,
        )
        page.wait_for_function("window.__phases.includes('speaking')", timeout=30_000)
        assert page.evaluate("document.getElementById('riva-status').dataset.working") == "false"
        assert not page.locator("#riva-working").is_visible()
        assert page.inner_text("#riva-status-note").strip() == ""
