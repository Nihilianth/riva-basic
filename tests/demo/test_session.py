"""Session lifecycle: commit gate, safe reset with in-flight work, speech control, privacy."""

import json
import threading
import time

import pytest
from support.demo import make_harness
from support.fakes import delayed_wire

from riva.demo.config import load_config
from riva.demo.fakes import turn_json
from riva.demo.session import SessionSettings

SECRET = "Geheimer Zoo-Satz"  # must never reach stdout or any ordinary log


def run_in_thread(fn, *args, **kwargs):
    box = {}

    def target():
        box["result"] = fn(*args, **kwargs)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def finish_run(run):
    for _ in run.updates():
        pass
    assert run.done.wait(5)


def test_a_turn_updates_journal_archive_and_starts_the_reply(tmp_path, capsys) -> None:
    h = make_harness(tmp_path)
    h.synth.release.set()
    report = h.answer(SECRET)
    assert report is not None and report.change.kind == "appended"
    assert report.journal_text == "Ich war im Zoo."  # the scripted model's passage
    finish_run(report.run)
    assert report.run.status == "completed"
    assert h.names() == [
        "session_started", "turn_started", "llm_response", "journal_applied", "reply_started",
    ]  # fmt: skip
    utterance = json.loads(
        (h.archive.directory / "utterances" / f"{report.utterance_id}.json").read_text()
    )
    assert utterance["verbatim_asr"] == [SECRET] and utterance["merged_transcript"] == SECRET
    assert SECRET in utterance["llm_input"] and utterance["llm_raw_outputs"]
    assert utterance["settings"]["persona"] == "expert" and utterance["spoken_reply"]
    journal = json.loads((h.archive.directory / "journal" / "0001.json").read_text())
    assert journal["passages"][0]["text"] == "Ich war im Zoo."
    out = capsys.readouterr().out
    assert SECRET not in out and "Ich war im Zoo." not in out  # ordinary log stays content-free


def test_history_and_used_words_flow_into_the_next_turn(tmp_path) -> None:
    vocab = turn_json(
        spoken_reply="Kennst du das Wort Rüssel? Das ist die lange Nase. Was hat er gemacht?",
        vocabulary={"word": "Rüssel", "explanation": "die lange Nase"},
    )
    h = make_harness(tmp_path, [vocab, turn_json()])
    h.synth.release.set()
    h.answer("Der Elefant hat Wasser gespritzt.")
    h.answer("Dann bin ich weitergegangen.")
    second_input = str(h.llm.calls[1]["input"])
    assert "Rüssel" in second_input and "Dann bin ich weitergegangen." in second_input
    assert "Der Elefant hat Wasser gespritzt." in second_input  # history carries the first turn


def test_reset_before_the_llm_call_discards_the_turn(tmp_path) -> None:
    h = make_harness(tmp_path)
    token = h.session.token()
    h.session.close()
    assert h.answer("Hallo", token=token) is None
    assert h.llm.calls == [] and h.session.journal.passages == []
    assert "stale_discarded" in h.names()


def test_reset_during_the_llm_call_drops_its_result_and_audits_it(tmp_path) -> None:
    h = make_harness(tmp_path, gated_llm=True)
    thread, box = run_in_thread(h.answer, "Hallo")
    assert h.llm.entered.wait(5)
    h.session.close()  # participant reloads while the model is still thinking
    h.llm.release.set()
    thread.join(5)
    assert not thread.is_alive() and box["result"] is None
    assert h.session.journal.passages == [] and h.replies.runs == {}
    events = h.events()
    stale = [e for e in events if e["event"] == "stale_discarded"]
    assert stale and stale[0]["stage"] == "after_llm"
    assert "journal_applied" not in h.names() and "reply_started" not in h.names()
    assert not (h.archive.directory / "utterances").exists()


def test_reset_during_tts_cancels_the_reply_and_stops_the_threads(tmp_path) -> None:
    h = make_harness(tmp_path)
    report = h.answer("Hallo")
    assert report is not None and h.synth.entered.wait(5)  # TTS is blocked mid-request
    run = report.run
    h.session.close()
    assert run.status == "cancelled" and run.cancelled.is_set()
    h.synth.release.set()
    run.producer.join(5)
    run.synthesizer.join(5)
    assert not run.producer.is_alive() and not run.synthesizer.is_alive()
    assert h.replies.runs == {}


def test_reset_during_asr_discards_the_segment_without_errors(tmp_path) -> None:
    h = make_harness(tmp_path, wire=delayed_wire(1.5))
    from support.demo import SyntheticWire  # noqa: F401  (ensures wire import path stays valid)

    payload = json.dumps(
        {
            "pcm16_b64": __import__("base64").b64encode(b"\0\0" * 16000).decode("ascii"),
            "started_ms": 10.0,
            "events": [{"stage": "vad.speech_end", "at_ms": 900.0}],
        }
    )
    token = h.session.token()
    result = {}

    def consume():
        gen = h.session.segment_updates(payload, token)
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            result["outcome"] = stop.value

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while h.asr.turns.get(h.session.identity.session_id) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    h.session.close()
    thread.join(10)
    assert not thread.is_alive() and result["outcome"] is None
    assert h.session.journal.passages == []
    assert {"stale_discarded"} & set(h.names())


def test_reset_drops_transcripts_that_were_held_for_the_next_turn(tmp_path) -> None:
    h = make_harness(tmp_path)
    h.asr.hold(h.session.identity, ["noch unbeantwortet"])
    h.session.close()
    assert h.asr.take_held(h.session.identity) == []


def test_close_is_idempotent_and_sessions_are_independent(tmp_path) -> None:
    a = make_harness(tmp_path / "a")
    b = make_harness(tmp_path / "b")
    b.synth.release.set()
    a.session.close()
    a.session.close("again")
    assert a.names().count("session_closed") == 1
    report = b.answer("Hallo")  # a reset in session A does not touch session B
    assert report is not None and not b.session.closed
    assert json.loads((a.archive.directory / "session.json").read_text())["close_reason"] == "reset"


def test_stopp_ends_the_reply_and_the_next_turn_knows_it_was_not_heard(tmp_path) -> None:
    h = make_harness(tmp_path)
    report = h.answer("Hallo")
    assert report is not None and h.synth.entered.wait(5)
    assert h.session.interrupt() is True
    assert report.run.status == "cancelled" and h.session.last_reply_heard is False
    h.synth.release.set()
    h.answer("Noch etwas.")
    assert "nicht ganz gehört" in str(h.llm.calls[1]["input"])
    assert "interrupted" in h.names()
    h.session.close()
    assert h.session.interrupt() is False  # a closed session cannot be interrupted


def test_barge_in_while_the_reply_should_still_play_counts_as_not_heard(tmp_path) -> None:
    h = make_harness(tmp_path)
    h.synth.release.set()
    report = h.answer("Hallo")
    finish_run(report.run)
    h.session.note_user_speech_started()  # 2 s of audio, user speaks right away
    assert h.session.last_reply_heard is False and "reply_interrupted" in h.names()
    later = make_harness(tmp_path / "later")
    later.synth.release.set()
    done = later.answer("Hallo")
    finish_run(done.run)
    later.session.monotonic = lambda: time.monotonic() + 60  # long after the audio ended
    later.session.note_user_speech_started()
    assert later.session.last_reply_heard is True


def test_a_doubtful_correction_is_answered_with_a_clarification(tmp_path) -> None:
    original = "Ich habe Elefanten und Löwen gesehen."
    guess = turn_json(
        journal_op={"type": "revise", "text": "Ich habe vier Elefanten gesehen.",
                    "target_id": "p0", "evidence": "es waren vier"},
        uncertain=True,
        spoken_reply="Danke, vier Elefanten also.",
    )  # fmt: skip
    first = turn_json(
        journal_op={"type": "append", "text": original, "target_id": None, "evidence": None}
    )
    h = make_harness(tmp_path, [first, guess])
    h.synth.release.set()
    h.answer(original)
    report = h.answer("Nein, es waren vier.")
    assert report.change.kind == "unchanged" and "?" in report.reply_text
    assert h.session.journal.passages[0].text == original
    assert "revise_blocked" in h.names()
    # the utterance that asked for the change is preserved in the archive
    record = json.loads(
        (h.archive.directory / "utterances" / f"{report.utterance_id}.json").read_text()
    )
    assert record["merged_transcript"] == "Nein, es waren vier."
    assert record["journal_change"]["reason"] == "revise_blocked_uncertain"


@pytest.mark.parametrize(
    "settings,message",
    [
        (SessionSettings("nobody", "gpt-4.1-mini", "coral"), "unknown persona"),
        (SessionSettings("expert", "gpt-9-turbo", "coral"), "model is not allowed"),
        (SessionSettings("expert", "gpt-4.1-mini", "robot"), "voice is not allowed"),
    ],
)
def test_choices_outside_the_allowlists_fail_early(tmp_path, settings, message) -> None:
    with pytest.raises(Exception, match=message):
        make_harness(tmp_path, settings=settings)


def test_every_persona_and_model_combination_makes_a_valid_session(tmp_path) -> None:
    config = load_config()
    for persona in config.personas:
        for model in config.models:
            h = make_harness(
                tmp_path / f"{persona.id}-{model.id}",
                settings=SessionSettings(persona.id, model.id, config.default_voice),
            )
            h.synth.release.set()
            assert h.answer("Hallo") is not None
            assert persona.prompt in str(h.llm.calls[0]["instructions"])
            assert h.llm.calls[0]["model"] == model.id


def test_a_revision_is_recorded_with_its_utterance_in_the_journal_snapshot(tmp_path) -> None:
    first = turn_json(
        journal_op={"type": "append", "text": "Ich habe zwei Elefanten gesehen.",
                    "target_id": None, "evidence": None}
    )  # fmt: skip
    fix = turn_json(
        journal_op={"type": "revise", "text": "Ich habe drei Elefanten gesehen.",
                    "target_id": "p0", "evidence": "es waren drei"}
    )  # fmt: skip
    h = make_harness(tmp_path, [first, fix])
    h.synth.release.set()
    h.answer("Ich habe zwei Elefanten gesehen.")
    report = h.answer("Nein, es waren drei Elefanten.")
    assert report.change.kind == "revised" and "passage_revised" in h.names()
    passage = json.loads((h.archive.directory / "journal" / "0002.json").read_text())["passages"][0]
    assert passage["text"] == "Ich habe drei Elefanten gesehen."
    assert passage["revisions"][0]["previous_text"] == "Ich habe zwei Elefanten gesehen."
    assert passage["revisions"][0]["utterance_text"] == "Nein, es waren drei Elefanten."
