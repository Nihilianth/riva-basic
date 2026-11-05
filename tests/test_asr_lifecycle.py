"""ASR lifecycle: the transcript is ready BEFORE the socket is closed (riva-42l, proposal A).

Two separate states: ``done`` (transcript final or failed) and ``connection_closed`` (socket
gone, tracked by ``connection_state``). Cleanup stays reliable and bounded: errors, cancellation,
a hanging or failing close, shutdown and session isolation all keep working.
"""

import base64
import json
import threading
import time

import numpy as np
from support.demo import make_harness
from support.fakes import FailingWire

from riva.audio.recordings import RecordingStore
from riva.audio.transcription import AsrConfig, LiveTurn
from riva.experiments.asr_stream import AsrSessions
from riva.experiments.vad_self_test import SyntheticWire

AUDIO = (16000, np.zeros(16000, dtype=np.float32))


class BlockingClose(SyntheticWire):
    """Synthetic ASR whose closing handshake does not finish until the test allows it."""

    def __init__(self) -> None:
        super().__init__()
        self.close_started = threading.Event()
        self.allow_close = threading.Event()

    def close(self) -> None:
        self.close_started.set()
        self.allow_close.wait(10)
        super().close()


class FailingClose(SyntheticWire):
    def close(self) -> None:
        raise OSError("sensitive close failure")


def turn_for(wire) -> LiveTurn:
    return LiveTurn(AsrConfig(), lambda: wire)


def test_the_transcript_is_ready_while_the_socket_is_still_closing() -> None:
    wire = BlockingClose()
    turn = turn_for(wire)
    try:
        turn.append(AUDIO)
        started = time.monotonic()
        result = turn.finish()
        assert result.status == "completed" and result.text == "Synthetischer Testlauf."
        assert time.monotonic() - started < 3  # not delayed by the closing handshake
        assert wire.close_started.wait(2)
        assert turn.connection_state == "closing" and not turn.connection_closed.is_set()
        assert turn.done.is_set()
        wire.allow_close.set()
        assert turn.wait_closed(2) and turn.connection_state == "closed" and wire.closed
        assert turn.finish() == result
    finally:
        wire.allow_close.set()
        turn.close()


def test_a_provider_error_is_final_and_still_closes_the_socket() -> None:
    wire = FailingWire()
    turn = turn_for(wire)
    try:
        turn.append(AUDIO)
        result = turn.finish()
        assert result.status == "failed" and result.error_code
        assert turn.wait_closed(2) and wire.closed and turn.connection_state == "closed"
    finally:
        turn.close()


def test_cancelling_before_the_result_closes_the_socket_within_a_bound() -> None:
    wire = SyntheticWire()
    turn = turn_for(wire)
    turn.append(AUDIO)  # audio sent, never committed
    started = time.monotonic()
    turn.close()
    assert time.monotonic() - started < 4
    assert turn.wait_closed(2) and wire.closed and turn.snapshot().status == "failed"


def test_a_close_that_hangs_is_tracked_and_never_blocks_close_or_the_caller() -> None:
    wire = BlockingClose()
    turn = turn_for(wire)
    turn.append(AUDIO)
    assert turn.finish().status == "completed"
    started = time.monotonic()
    turn.close()  # transcript is final: must not wait for the hanging handshake
    assert time.monotonic() - started < 0.5
    assert not turn.wait_closed(0.2) and turn.connection_state == "closing"
    wire.allow_close.set()
    assert turn.wait_closed(2) and turn.connection_state == "closed"


def test_a_failing_close_is_contained_and_reported_as_a_state() -> None:
    wire = FailingClose()
    turn = turn_for(wire)
    try:
        turn.append(AUDIO)
        assert turn.finish().status == "completed"  # the transcript is unaffected
        assert turn.wait_closed(2) and turn.connection_state == "close_failed"
        assert "sensitive" not in repr(turn.snapshot())
    finally:
        turn.close()


def test_a_connection_that_never_opened_has_nothing_to_close() -> None:
    def broken():
        raise OSError("no network")

    turn = LiveTurn(AsrConfig(), broken)
    turn.append(AUDIO)
    assert turn.finish().status == "failed"
    assert turn.wait_closed(2) and turn.connection_state == "closed"


def test_shutdown_cancels_all_turns_and_reports_what_is_still_closing(tmp_path) -> None:
    wires = []

    def factory():
        wires.append(BlockingClose())
        return wires[-1]

    sessions = AsrSessions(RecordingStore(tmp_path / "a"), AsrConfig(), factory)
    from riva.audio.recordings import new_development_session

    identities = [new_development_session() for _ in range(2)]
    for identity in identities:
        sessions.begin(identity)
        sessions.append(AUDIO, identity)
        sessions.current(identity).stopped = True
        sessions.finish(identity)  # transcript final, sockets still closing (blocked)
    assert sessions.close_all(timeout=0.3) == 2  # bounded: reports, does not hang
    for wire in wires:
        wire.allow_close.set()
    assert sessions.close_all(timeout=3) == 0
    assert all(w.closed for w in wires)


def test_the_next_utterance_and_a_stopp_do_not_wait_for_a_closing_socket(tmp_path) -> None:
    wires = []

    def factory():
        wires.append(BlockingClose())
        return wires[-1]

    h = make_harness(tmp_path, wire=factory)
    payload = json.dumps(
        {
            "pcm16_b64": base64.b64encode(b"\0\0" * 16000).decode("ascii"),
            "started_ms": 10.0,
            "events": [{"stage": "vad.speech_end", "at_ms": 900.0}],
        }
    )

    def run_segment():
        gen = h.session.segment_updates(payload, h.session.token())
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            return stop.value

    first = run_segment()
    assert first is not None and not wires[0].closed  # first socket still closing
    started = time.monotonic()
    assert h.session.interrupt() is True  # Stopp does not wait for it
    second = run_segment()  # neither does the next utterance
    assert second is not None and time.monotonic() - started < 3
    assert not wires[0].closed
    for wire in wires:
        wire.allow_close.set()
    h.session.close()
    assert h.asr.close_all(timeout=3) == 0


def test_runtime_shutdown_closes_sessions_and_waits_a_bounded_time(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from riva.audio.recordings import new_development_session
    from riva.demo.app import build_demo_api, build_runtime
    from riva.demo.config import load_config
    from riva.demo.providers import Providers

    wires = []

    def factory():
        wires.append(BlockingClose())
        return wires[-1]

    from support.demo import GatedSynth

    from riva.demo.fakes import EchoTurnLLM

    synth = GatedSynth()
    synth.release.set()
    rt = build_runtime(
        load_config(), Providers(EchoTurnLLM(), synth, factory), tmp_path / "archive"
    )
    session = rt.ensure(new_development_session())
    sessions_asr = rt.asr
    sessions_asr.begin(session.identity)
    sessions_asr.append(AUDIO, session.identity)
    sessions_asr.current(session.identity).stopped = True
    sessions_asr.finish(session.identity)
    with TestClient(build_demo_api(rt)):
        pass  # leaving the context fires the application's shutdown handlers
    assert session.closed and session.close_reason == "shutdown"
    for wire in wires:
        wire.allow_close.set()
    assert rt.asr.close_all(timeout=3) == 0
    assert rt.shutdown(timeout=1) == 0  # idempotent
