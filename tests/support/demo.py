"""Build a DemoSession with gated deterministic fakes (events instead of sleeps)."""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from riva.audio.recordings import RecordingStore, SavedRecording, new_development_session
from riva.audio.replies import ReplyConfig, ReplyManager
from riva.audio.transcription import AsrConfig
from riva.demo.archive import SessionArchive
from riva.demo.config import load_config
from riva.demo.fakes import ScriptedTurnLLM, turn_json
from riva.demo.providers import SpokenReplyProvider
from riva.demo.session import DemoSession, SessionSettings
from riva.demo.turn import LLMReply, TurnProcessor
from riva.experiments.asr_stream import AsrSessions
from riva.experiments.vad_self_test import SyntheticWire


class GatedLLM(ScriptedTurnLLM):
    """Blocks inside ``respond`` until the test releases it."""

    def __init__(self, replies: Any) -> None:
        super().__init__(replies)
        self.entered = threading.Event()
        self.release = threading.Event()

    def respond(self, instructions: str, user_input: str, **kwargs: Any) -> LLMReply:
        self.entered.set()
        assert self.release.wait(10), "test never released the LLM"
        return super().respond(instructions, user_input, **kwargs)


class GatedSynth:
    """Text-to-speech that blocks before its first audio until released."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.phrases: list[str] = []

    def speech(self, phrase: str, config: ReplyConfig) -> Any:
        self.phrases.append(phrase)
        self.entered.set()
        self.release.wait(10)
        yield b"\0\0" * 12000


@dataclass
class DemoHarness:
    session: DemoSession
    asr: AsrSessions
    replies: ReplyManager
    archive: SessionArchive
    llm: ScriptedTurnLLM
    synth: GatedSynth
    root: Path

    def saved(self, name: str = "x") -> SavedRecording:
        store = RecordingStore(self.root / "archive")
        return store.save((16000, np.zeros(16000, dtype=np.int16)), self.session.identity)

    def answer(self, text: str, token: Any = None, **kwargs: Any) -> Any:
        saved = self.saved()
        return self.session.answer(
            text, saved, [text], [saved.metadata.turn_id], token or self.session.token(), **kwargs
        )

    def events(self) -> list[dict[str, Any]]:
        import json

        path = self.archive.directory / "timeline.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def names(self) -> list[str]:
        return [event["event"] for event in self.events()]


def make_harness(
    tmp_path: Path,
    replies: Any = None,
    *,
    gated_llm: bool = False,
    wire: Any = SyntheticWire,
    settings: SessionSettings | None = None,
) -> DemoHarness:
    config = load_config()
    identity = new_development_session()
    script = replies if replies is not None else [turn_json()] * 8
    llm: ScriptedTurnLLM = GatedLLM(script) if gated_llm else ScriptedTurnLLM(script)
    synth = GatedSynth()
    store = RecordingStore(tmp_path / "archive")
    asr = AsrSessions(store, AsrConfig(), wire)
    manager = ReplyManager(ReplyConfig(), lambda: SpokenReplyProvider(synth))
    archive = SessionArchive(tmp_path / "archive", identity)
    session = DemoSession(
        identity=identity,
        config=config,
        settings=settings or SessionSettings("expert", "gpt-4.1-mini", "coral"),
        processor=TurnProcessor(llm, config),
        asr=asr,
        replies=manager,
        archive=archive,
    )
    return DemoHarness(session, asr, manager, archive, llm, synth, tmp_path)
