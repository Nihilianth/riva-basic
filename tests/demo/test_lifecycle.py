"""Session lifecycle: creating is not closing.

A reload isolates a new session, a presenter reset closes at once, abandoned sessions expire.
"""

import json
import threading

import pytest
from support.demo import GatedSynth
from support.fakes import FailingWire  # noqa: F401  (kept importable for lifecycle failure tests)

from riva.audio.recordings import new_development_session
from riva.demo.app import KEEP_CLOSED, build_runtime
from riva.demo.config import load_config
from riva.demo.fakes import EchoTurnLLM
from riva.demo.providers import Providers
from riva.experiments.vad_self_test import SyntheticWire


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def runtime(tmp_path, clock=None, idle=180.0, llm=None):
    clock = clock or Clock()
    providers = Providers(llm or EchoTurnLLM(), GatedSynth(), SyntheticWire)
    providers.speech.release.set()
    rt = build_runtime(
        load_config(), providers, tmp_path / "archive", idle_timeout=idle, monotonic=clock
    )
    return rt, clock


def timeline(session):
    path = session.archive.directory / "timeline.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_each_page_identity_gets_its_own_isolated_session(tmp_path) -> None:
    rt, _ = runtime(tmp_path)
    first, second = new_development_session(), new_development_session()
    a, b = rt.ensure(first), rt.ensure(second)
    assert a is not b and rt.ensure(first) is a  # same identity -> same session, never a copy
    assert a.archive.directory != b.archive.directory
    a.journal.passages.append(object())  # type: ignore[arg-type]  # private state is per session
    assert b.journal.passages == []
    assert len(rt.active()) == 2


def test_presenter_reset_closes_immediately_and_only_that_session(tmp_path) -> None:
    rt, _ = runtime(tmp_path)
    a, b = rt.ensure(new_development_session()), rt.ensure(new_development_session())
    assert rt.reset(a.identity.session_id) is True
    assert a.closed and a.close_reason == "presenter_reset" and not b.closed
    assert [e["event"] for e in timeline(a)][-1] == "session_closed"
    assert timeline(a)[-1]["reason"] == "presenter_reset"
    assert rt.reset(a.identity.session_id) is False  # already closed
    assert rt.reset("unknown-id") is False and rt.reset("../etc") is False
    assert rt.reset_all() == 1 and b.closed


def test_a_reloaded_page_closes_the_old_session_through_the_unload_beacon(tmp_path) -> None:
    rt, _ = runtime(tmp_path)
    old = rt.ensure(new_development_session())
    new = rt.ensure(new_development_session())  # the reloaded page has a fresh identity
    assert rt.end_session(old.identity.session_id) is True
    assert old.closed and old.close_reason == "page_unloaded" and not new.closed


def test_abandoned_sessions_expire_but_active_ones_do_not(tmp_path) -> None:
    rt, clock = runtime(tmp_path, idle=180.0)
    abandoned = rt.ensure(new_development_session())
    alive_identity = new_development_session()
    alive = rt.ensure(alive_identity)
    clock.now += 120
    rt.ensure(alive_identity)  # the live page keeps polling
    clock.now += 100  # abandoned: 220 s idle, alive: 100 s idle
    assert rt.reap() == [abandoned.identity.session_id]
    assert abandoned.closed and abandoned.close_reason == "expired" and not alive.closed
    assert rt.reap() == []  # nothing left to expire


def test_expiry_runs_by_itself_when_the_demo_is_used(tmp_path) -> None:
    rt, clock = runtime(tmp_path)
    abandoned = rt.ensure(new_development_session())
    clock.now += 400
    rt.ensure(new_development_session())  # any page activity triggers the periodic reap
    assert abandoned.closed and abandoned.close_reason == "expired"


def test_a_closed_session_is_never_recreated_and_rejects_late_results(tmp_path) -> None:
    rt, _ = runtime(tmp_path)
    identity = new_development_session()
    session = rt.ensure(identity)
    token = session.token()
    rt.release(identity)  # Gradio state cleanup, long after the page left
    rt.release(identity)  # idempotent
    assert rt.ensure(identity) is session and session.closed  # a late poll must not resurrect it
    assert session.view()["phase"] == "closed"
    saved_dir = session.archive.directory
    assert session.answer("Hallo", None, ["Hallo"], ["s"], token) is None  # type: ignore[arg-type]
    names = [e["event"] for e in timeline(session)]
    assert names.count("session_closed") == 1 and "stale_discarded" in names
    assert session.journal.passages == [] and saved_dir.is_dir()


def test_expiry_during_an_llm_call_discards_the_result(tmp_path) -> None:
    gate = threading.Event()
    entered = threading.Event()

    class Blocking(EchoTurnLLM):
        def respond(self, *args, **kwargs):
            entered.set()
            assert gate.wait(10)
            return super().respond(*args, **kwargs)

    from support.demo import make_harness  # same gating style as the session tests

    rt, clock = runtime(tmp_path, llm=Blocking())
    session = rt.ensure(new_development_session())
    h = make_harness(tmp_path / "h")
    saved = h.saved()
    box = {}

    def go():
        box["r"] = session.answer(
            "Hallo", saved, ["Hallo"], [saved.metadata.turn_id], session.token()
        )

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    assert entered.wait(5)
    clock.now += 500
    assert rt.reap() == [session.identity.session_id]  # the page vanished while the model thinks
    gate.set()
    thread.join(5)
    assert box["r"] is None and session.journal.passages == []
    events = timeline(session)
    assert [e["stage"] for e in events if e["event"] == "stale_discarded"] == ["after_llm"]
    assert events[-1]["event"] in ("stale_discarded", "session_closed")


def test_old_closed_sessions_are_pruned_but_recent_ones_stay(tmp_path) -> None:
    rt, _ = runtime(tmp_path)
    ids = []
    for _ in range(KEEP_CLOSED + 5):
        identity = new_development_session()
        ids.append(identity)
        rt.ensure(identity).close("test")
    rt.ensure(new_development_session())
    closed = [s for s in rt.sessions.values() if s.closed]
    assert len(closed) <= KEEP_CLOSED
    assert rt.sessions.get(ids[-1].session_id) is not None


def test_the_unload_beacon_endpoint_closes_a_session_and_ignores_garbage(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from riva.demo.app import build_demo_api

    rt, _ = runtime(tmp_path)
    session = rt.ensure(new_development_session())
    client = TestClient(build_demo_api(rt))
    assert client.post("/riva/session_end", json={"session_id": "nope"}).json() == {"closed": False}
    assert client.post("/riva/session_end", content="not json").json() == {"closed": False}
    sid = session.identity.session_id
    assert client.post("/riva/session_end", json={"session_id": sid}).json() == {"closed": True}
    assert session.closed and session.close_reason == "page_unloaded"
    assert client.post("/riva/session_end", json={"session_id": sid}).json() == {"closed": False}


@pytest.mark.parametrize("bad", ["", "a b", "x" * 65, "../x"])
def test_reset_rejects_malformed_ids(tmp_path, bad) -> None:
    rt, _ = runtime(tmp_path)
    assert rt.reset(bad) is False
