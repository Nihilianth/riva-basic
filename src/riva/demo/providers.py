"""Provider seams for the demo: ASR wire, turn LLM and speech synthesis, real or fake."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol

from riva.audio.replies import ReplyConfig, TextPart
from riva.audio.transcription import Wire
from riva.demo.turn import TurnLLM


class SpeechSynth(Protocol):
    def speech(self, phrase: str, config: ReplyConfig) -> Iterator[bytes]: ...


@dataclass(frozen=True)
class Providers:
    """Everything that talks to the outside world. Tests and ``--fake`` swap the whole bundle."""

    llm: TurnLLM
    speech: SpeechSynth
    wire_factory: Callable[[], Wire]


class SpokenReplyProvider:
    """``ReplyProvider`` for the existing ``ReplyRun``: the reply text is already decided (by the
    turn LLM) and passed in as the run's "transcript"; this streams it back as one text part and
    synthesizes speech phrase by phrase. One instance can serve every session."""

    def __init__(self, synth: SpeechSynth) -> None:
        self.synth = synth

    def text(self, transcript: str, config: ReplyConfig) -> Iterator[TextPart]:
        yield TextPart("delta", transcript)
        yield TextPart("completed", transcript)

    def speech(self, phrase: str, config: ReplyConfig) -> Iterator[bytes]:
        return self.synth.speech(phrase, config)


def real_providers() -> Providers:
    """OpenAI ASR, LLM and TTS (paid; needs OPENAI_API_KEY in the environment)."""
    from riva.audio.replies import OpenAIReplyProvider
    from riva.audio.transcription import connect_openai
    from riva.demo.openai_llm import OpenAITurnLLM

    return Providers(OpenAITurnLLM(), OpenAIReplyProvider(), connect_openai)


def fake_providers() -> Providers:
    """Offline and deterministic: synthetic ASR text, echo journal LLM, tone speech."""
    from riva.demo.fakes import EchoTurnLLM
    from riva.experiments.vad_self_test import SyntheticReplyProvider, SyntheticWire

    return Providers(EchoTurnLLM(), SyntheticReplyProvider(), SyntheticWire)
