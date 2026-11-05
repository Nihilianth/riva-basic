"""Stopp ends the current interaction in ANY state through the session epoch/commit gate.

Every stage in flight when Stopp was pressed is stale afterwards: its result is discarded and
audited, never applied. Committed journal entries and the audit trail stay.
"""

import base64
import json
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from support.demo import make_harness
from support.fakes import delayed_wire

from riva.demo.fakes import ScriptedTurnLLM, turn_json
from riva.experiments.voice_reply import add_reply_audio_route


def go(fn, *args, **kwargs):
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


def segment_payload(end_ms=900.0):
    return json.dumps(
        {
            "pcm16_b64": base64.b64encode(b"\0\0" * 16000).decode("ascii"),
            "started_ms": 10.0,
            "events": [{"stage": "vad.speech_end", "at_ms": end_ms}],
        }
    )


def drain(generator):
    try:
        while True:
            next(generator)
    except StopIteration as stop:
        return stop.value


def audio_client(h):
    api = FastAPI()
    add_reply_audio_route(api, h.replies)
    return TestClient(api)


def test_stopp_with_nothing_running_is_harmless_and_repeatable(tmp_path) -> None:
    h = make_harness(tmp_path)
    h.synth.release.set()
    for _ in range(3):
        assert h.session.interrupt() is True
    assert h.session.epoch == 3 and h.session.phase == "stopped" and not h.session.closed
    assert h.names().count("interrupted") == 3
    report = h.answer("Danach geht es normal weiter.")  # a fresh token works again
    assert report is not None and h.session.journal.passages[0].text == "Ich war im Zoo."


def test_a_turn_stopped_before_the_llm_call_never_calls_the_model(tmp_path) -> None:
    h = make_harness(tmp_path)
    old = h.session.token()
    h.session.interrupt()
    assert h.answer("Hallo", token=old) is None
    assert h.llm.calls == [] and h.session.journal.passages == []
    assert [e["stage"] for e in h.events() if e["event"] == "stale_discarded"] == ["before_llm"]


def test_a_stopped_llm_result_cannot_change_the_journal_or_start_playback(tmp_path) -> None:
    h = make_harness(tmp_path, gated_llm=True)
    thread, box = go(h.answer, "Hallo")
    assert h.llm.entered.wait(5)
    h.session.interrupt()  # Stopp while the model is thinking
    h.llm.release.set()  # ... and the answer arrives late
    thread.join(5)
    assert box["result"] is None and h.session.journal.passages == []
    assert h.replies.runs == {} and not (h.archive.directory / "utterances").exists()
    names = h.names()
    assert names.index("interrupted") < names.index("stale_discarded")
    assert "journal_applied" not in names and "reply_started" not in names
    assert [e["stage"] for e in h.events() if e["event"] == "stale_discarded"] == ["after_llm"]
    assert not h.session.closed and h.session.phase == "stopped"


def test_a_stopped_asr_result_is_discarded_but_the_recording_stays_in_the_audit_trail(
    tmp_path,
) -> None:
    h = make_harness(tmp_path, wire=delayed_wire(1.0))
    token = h.session.token()
    thread, box = go(lambda: drain(h.session.segment_updates(segment_payload(), token)))
    deadline = time.monotonic() + 5
    while h.asr.turns.get(h.session.identity.session_id) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    h.session.interrupt()  # Stopp while the speech is being recognized
    thread.join(10)
    assert box["result"] is None and h.session.verbatim == [] and h.session.journal.passages == []
    assert "segment_saved" not in h.names()  # never reported as a usable segment
    recordings = list(h.archive.directory.glob("*/recording.wav"))
    assert len(recordings) == 1  # ... but the audio that was received is preserved
    assert any(e["event"] == "stale_discarded" for e in h.events())
    # The next utterance is processed normally (it waits for the interrupted one to be saved).
    fresh = drain(h.session.segment_updates(segment_payload(), h.session.token()))
    assert fresh is not None and fresh.result.text == "Synthetischer Testlauf."


def test_a_stopp_between_asr_and_the_gate_leaves_no_held_text_behind(tmp_path, monkeypatch) -> None:
    from riva.demo import session as session_module

    h = make_harness(tmp_path)
    token = h.session.token()
    outcome = drain(h.session.segment_updates(segment_payload(), token))
    assert outcome is not None
    h.asr.note_speech(h.session.identity, "start", 5000.0)  # the user talks on: the gate would hold
    real_gate = session_module.gate_transcript

    def gate_then_stopp(*args, **kwargs):
        decision = real_gate(*args, **kwargs)  # holds the text ...
        h.session.interrupt()  # ... and Stopp lands right after it
        return decision

    monkeypatch.setattr(session_module, "gate_transcript", gate_then_stopp)
    assert h.session.on_segment(outcome, token) is None
    assert h.asr.take_held(h.session.identity) == [] and h.session._pending == []
    assert h.llm.calls == []


def test_stopp_drops_fragments_that_were_waiting_for_more_speech(tmp_path) -> None:
    h = make_harness(tmp_path)
    h.synth.release.set()
    token = h.session.token()
    first = drain(h.session.segment_updates(segment_payload(900.0), token))
    h.asr.note_speech(h.session.identity, "start", 5000.0)  # speech began after this segment ended
    assert h.session.on_segment(first, token) == "held"
    assert h.session._pending and h.asr.held_preview(h.session.identity)
    h.session.interrupt()
    assert h.session._pending == [] and h.asr.held_preview(h.session.identity) == []
    # What the participant says next is answered on its own, not merged with the dropped fragment.
    h.asr.note_speech(h.session.identity, "end", 6000.0)
    token2 = h.session.token()
    second = drain(h.session.segment_updates(segment_payload(9000.0), token2))
    report = h.session.on_segment(second, token2)
    assert report not in (None, "held", "unusable")
    assert json.dumps("Synthetischer Testlauf.") in str(h.llm.calls[-1]["input"])
    assert "Synthetischer Testlauf. Synthetischer Testlauf." not in str(h.llm.calls[-1]["input"])


def test_stopp_during_tts_cancels_the_reply_and_late_audio_never_plays(tmp_path) -> None:
    h = make_harness(tmp_path)
    report = h.answer("Hallo")
    assert report is not None and h.synth.entered.wait(5)  # TTS is blocked mid-request
    h.session.interrupt()
    assert report.run.status == "cancelled" and h.session.last_reply_heard is False
    h.synth.release.set()  # the provider now delivers its audio, too late
    report.run.producer.join(5)
    report.run.synthesizer.join(5)
    assert report.run.read_audio(0) == (b"", True)
    client = audio_client(h)
    response = client.get(f"/riva/reply_audio/{report.run.generation_id}")
    assert response.status_code == 200 and response.content == b""  # nothing for the player
    assert h.session.journal.passages[0].text == "Ich war im Zoo."  # committed entry stays


def test_stopp_during_playback_serves_no_buffered_audio_afterwards(tmp_path) -> None:
    h = make_harness(tmp_path)
    h.synth.release.set()
    report = h.answer("Hallo")
    finish_run(report.run)
    assert report.run.status == "completed" and report.run.read_audio(0)[0]  # audio exists
    client = audio_client(h)
    h.session.interrupt()  # the browser may still be playing it
    assert report.run.status == "cancelled"
    assert client.get(f"/riva/reply_audio/{report.run.generation_id}").content == b""
    assert h.session.journal.text() == "Ich war im Zoo."


def test_a_late_result_from_before_the_stopp_cannot_touch_the_next_turn(tmp_path) -> None:
    release_old, entered_old = threading.Event(), threading.Event()
    state = {"calls": 0}

    class FirstBlocks(ScriptedTurnLLM):
        def respond(self, instructions, user_input, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                entered_old.set()
                assert release_old.wait(10)
            # Answer by content: the call that finishes first is not the one that started first.
            self.replies.clear()
            self.replies.append(old_text if '"Alt"' in user_input else new_text)
            return super().respond(instructions, user_input, **kwargs)

    h = make_harness(tmp_path)
    old_text = turn_json(
        journal_op={"type": "append", "text": "Alter Satz.", "target_id": None, "evidence": None}
    )
    new_text = turn_json(
        journal_op={"type": "append", "text": "Neuer Satz.", "target_id": None, "evidence": None}
    )
    h.session.processor.llm = FirstBlocks([])
    h.synth.release.set()
    thread, box = go(h.answer, "Alt")
    assert entered_old.wait(5)
    h.session.interrupt()
    fresh = h.answer("Neu")  # the participant starts over while the old call is still running
    assert fresh is not None and h.session.journal.text() == "Neuer Satz."
    release_old.set()  # the old answer finally arrives
    thread.join(5)
    assert box["result"] is None
    assert h.session.journal.text() == "Neuer Satz." and len(h.session.journal.passages) == 1
    assert h.session.last_run is fresh.run and fresh.run.status != "cancelled"
    assert len(list((h.archive.directory / "utterances").glob("*.json"))) == 1


@pytest.mark.parametrize("round_", range(40))
def test_stopp_racing_the_commit_never_leaves_a_live_reply_without_its_entry(
    tmp_path, round_
) -> None:
    """Whichever wins, the end state is consistent: stopped before commit -> nothing applied;
    stopped after commit -> entry kept and the reply cancelled. Never a live reply after Stopp."""
    h = make_harness(tmp_path)
    h.synth.release.set()
    barrier = threading.Barrier(2)
    token = h.session.token()  # captured when the stage starts, like the real handler does

    def answer():
        barrier.wait()
        return h.answer("Hallo", token=token)

    thread, box = go(answer)
    barrier.wait()
    if round_ % 2:
        time.sleep(0.001 * (round_ % 5))
    h.session.interrupt()
    thread.join(10)
    report = box["result"]
    if report is None:
        assert h.session.journal.passages == [] and h.replies.runs == {}
    else:
        assert len(h.session.journal.passages) == 1  # committed entries survive
        assert report.run is not None and report.run.status == "cancelled"
    assert h.session.phase == "stopped" and not h.session.closed
    assert h.names().count("interrupted") == 1


def test_the_audit_trail_survives_a_stopp(tmp_path) -> None:
    h = make_harness(tmp_path)
    report = h.answer("Hallo")
    assert h.synth.entered.wait(5)
    h.session.interrupt()
    h.synth.release.set()
    names = h.names()
    assert names == [
        "session_started", "turn_started", "llm_response", "journal_applied",
        "reply_started", "reply_interrupted", "interrupted",
    ]  # fmt: skip
    record = json.loads(
        (h.archive.directory / "utterances" / f"{report.utterance_id}.json").read_text()
    )
    assert record["merged_transcript"] == "Hallo" and record["spoken_reply"]
    assert json.loads((h.archive.directory / "journal" / "0001.json").read_text())["passages"]
    interrupted = [e for e in h.events() if e["event"] == "interrupted"][0]
    assert interrupted["reason"] == "stopped" and interrupted["generation"]


def test_stale_stages_cannot_change_what_the_indicator_shows(tmp_path) -> None:
    h = make_harness(tmp_path)
    old = h.session.token()
    h.session.interrupt()
    h.session.set_phase("listening", old)  # a stopped stage tries to say "listening"
    h.session.set_phase("preparing", old)
    assert h.session.phase == "stopped"
    h.session.resume()
    assert h.session.phase == "listening"
    h.session.set_phase("thinking", h.session.token())
    h.session.resume()  # Start pressed while not stopped changes nothing
    assert h.session.phase == "thinking"
