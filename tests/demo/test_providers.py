import numpy as np

from riva.audio.recordings import RecordingStore, new_development_session
from riva.audio.replies import ReplyConfig, ReplyManager
from riva.demo.config import load_config
from riva.demo.prompts import TurnContext
from riva.demo.providers import SpokenReplyProvider, fake_providers
from riva.demo.turn import TurnProcessor


def test_spoken_reply_provider_streams_the_decided_text_and_synthesizes_each_phrase(tmp_path):
    spoken = "Das klingt toll. Was hast du dort gesehen?"
    calls = []

    class Synth:
        def speech(self, phrase, config):
            calls.append((phrase, config.voice, config.tts_style))
            yield b"\0\0" * 12000

    identity = new_development_session()
    saved = RecordingStore(tmp_path).save((24000, np.zeros(12000, dtype=np.int16)), identity)
    manager = ReplyManager(ReplyConfig(), lambda: SpokenReplyProvider(Synth()))
    config = ReplyConfig(voice="marin", tts_style="Sprich warm.")
    run, _ = manager.start(identity, saved, spoken, 0, config)
    try:
        for _ in run.updates():
            pass
        assert run.done.wait(2) and run.status == "completed" and run.text == spoken
        assert [c[0] for c in calls] == ["Das klingt toll.", "Was hast du dort gesehen?"]
        assert {(c[1], c[2]) for c in calls} == {("marin", "Sprich warm.")}
    finally:
        manager.release(identity)


def test_fake_providers_work_end_to_end_offline() -> None:
    providers = fake_providers()
    outcome = TurnProcessor(providers.llm, load_config()).process(
        TurnContext(transcript="Heute war ich im Zoo."), persona_id="expert", model="m"
    )
    assert outcome.source == "llm" and outcome.journal_op.text == "Heute war ich im Zoo."
    assert b"".join(providers.speech.speech("Hallo.", ReplyConfig()))
    assert providers.wire_factory() is not None
