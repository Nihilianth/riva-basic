"""Real MicVAD in headless Chromium, fed by prerecorded synthetic German clips.

ASR, LLM and TTS are deterministic fakes; everything else (VAD model, hidden submit button, Gradio
queue, backend hold/merge gate, reply-audio endpoint, Web Audio player) is the real code.
"""

from pathlib import Path
from typing import Any

import pytest
from conftest import mic_browser  # type: ignore[import-not-found]
from support.audio import clip, compose, noise, silence
from support.fakes import delayed_wire, recording_provider
from support.server import running_voice_app

LISTENING = "window.rivaVad && (window.rivaVad.stages || []).includes('listening')"
FINAL_TEXT = (
    "() => [...document.querySelectorAll('textarea')]"
    ".some(t => t.value === 'Synthetischer Testlauf.')"
)


def wait_until_listening(page: Any) -> None:
    page.wait_for_function(LISTENING, timeout=90_000)


def test_speech_from_fake_microphone_reaches_asr_and_a_played_reply(
    tmp_path: Path, playwright_instance: Any
) -> None:
    wav = compose(tmp_path / "mic.wav", silence(1.0), clip("zoo"), silence(4.0))
    with (
        running_voice_app(tmp_path) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = context.new_page()
        page.goto(app.url)
        wait_until_listening(page)
        page.wait_for_function(FINAL_TEXT, timeout=60_000)
        page.wait_for_function(
            "window.rivaPlayerStats && window.rivaPlayerStats.chunks > 0", timeout=30_000
        )
        stages = page.evaluate("window.rivaVad.stages")
        assert "speech start" in stages and "speech confirmed" in stages
        assert any(s.startswith("speech end") for s in stages)
        assert len(app.sessions.turns) == 1
        (run,) = app.replies.runs.values()
        assert run.status in ("generating", "completed")


def test_background_noise_is_ignored_and_the_speech_after_it_is_heard(
    tmp_path: Path, playwright_instance: Any
) -> None:
    wav = compose(
        tmp_path / "mic.wav", silence(1.0), noise(3.0), silence(1.0), clip("zoo"), silence(4.0)
    )
    with (
        running_voice_app(tmp_path) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = context.new_page()
        page.goto(app.url)
        wait_until_listening(page)
        page.wait_for_function(FINAL_TEXT, timeout=60_000)
        assert len(app.sessions.turns) == 1  # the noise never became a segment


def test_a_fragmented_utterance_is_held_and_answered_as_one_turn(
    tmp_path: Path, playwright_instance: Any
) -> None:
    seen: list[str] = []
    wav = compose(
        tmp_path / "mic.wav",
        silence(1.0),
        clip("zoo"),
        silence(1.8),  # longer than the VAD pause threshold: two segments
        clip("elefanten"),
        silence(6.0),
    )
    with (
        running_voice_app(
            tmp_path, wire=delayed_wire(4.0), provider=recording_provider(seen)
        ) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = context.new_page()
        page.goto(app.url)
        wait_until_listening(page)
        page.wait_for_function(
            "window.rivaPlayerStats && window.rivaPlayerStats.chunks > 0", timeout=60_000
        )
        # Both fragments were transcribed, but the "LLM" received them together, once.
        assert seen == ["Synthetischer Testlauf. Synthetischer Testlauf."]


@pytest.mark.xfail(
    reason="riva-skm.37: the VAD deaf-while-playing guard suppresses barge-in; this test encodes "
    "the desired behaviour and should pass again once the guard lets corrections through.",
    strict=False,
)
def test_speaking_over_a_reply_stops_it_and_a_new_reply_follows(
    tmp_path: Path, playwright_instance: Any
) -> None:
    seen: list[str] = []
    wav = compose(
        tmp_path / "mic.wav",
        silence(1.0),
        clip("zoo"),
        silence(2.5),  # the first reply (about 6 s of audio) is playing by now
        clip("elefanten"),
        silence(6.0),
    )
    with (
        running_voice_app(
            tmp_path, provider=recording_provider(seen, seconds_per_phrase=3.0)
        ) as app,
        mic_browser(playwright_instance, wav) as context,
    ):
        page = context.new_page()
        page.goto(app.url)
        wait_until_listening(page)
        page.wait_for_function(
            "(window.rivaPlayerStats?.counts?.suspended_user_speaking || 0) >= 1", timeout=60_000
        )
        page.wait_for_function(
            "(window.rivaPlayerStats?.counts?.dropped_stale_reply || 0) >= 1", timeout=30_000
        )
        page.wait_for_function("window.rivaPlayerStats.counts.stream_started >= 2", timeout=30_000)
        assert len(seen) == 2
