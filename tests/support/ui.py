"""Helpers shared by the participant-page and conversation browser tests."""

import time
from typing import Any

from riva.demo.fakes import EchoTurnLLM
from riva.demo.providers import Providers
from riva.experiments.vad_self_test import SyntheticReplyProvider, SyntheticWire

LISTENING = "window.rivaVad && (window.rivaVad.stages || []).includes('listening')"
PHASE_RECORDER = """
window.__phases = [];
const recordPhase = () => {
  const box = document.getElementById('riva-status');
  if (box && window.__phases[window.__phases.length - 1] !== box.dataset.phase)
    window.__phases.push(box.dataset.phase);
};
new MutationObserver(recordPhase).observe(document, {
  subtree: true, childList: true, attributes: true, attributeFilter: ['data-phase']});
"""


class SlowEcho(EchoTurnLLM):
    delay = 1.2

    def respond(self, *args: Any, **kwargs: Any) -> Any:
        time.sleep(self.delay)
        return super().respond(*args, **kwargs)


class SlowTone(SyntheticReplyProvider):
    """Synthetic speech that needs a moment before its first audio and lasts several seconds."""

    seconds = 4

    def speech(self, phrase: str, config: Any) -> Any:
        time.sleep(0.8)
        for _ in range(self.seconds):
            yield from super().speech(phrase, config)


def providers(wire: Any = SyntheticWire, llm: Any = None, speech: Any = None) -> Providers:
    return Providers(llm or SlowEcho(), speech or SlowTone(), wire)


def is_subsequence(wanted: list[str], seen: list[str]) -> bool:
    it = iter(seen)
    return all(item in it for item in wanted)


def load(context: Any, url: str) -> Any:
    page = context.new_page()
    page.add_init_script(PHASE_RECORDER)
    page.goto(url)
    return page


class RecordingEcho(EchoTurnLLM):
    """Echo LLM that remembers every prompt it was given."""

    def __init__(self) -> None:
        self.inputs: list[str] = []

    def respond(self, instructions: str, user_input: str, **kwargs: Any) -> Any:
        self.inputs.append(user_input)
        return super().respond(instructions, user_input, **kwargs)


def demo_providers(llm: Any, wire: Any = SyntheticWire, seconds: int = 1) -> Providers:
    tone = type("Tone", (SlowTone,), {"seconds": seconds})()
    return Providers(llm, tone, wire)
