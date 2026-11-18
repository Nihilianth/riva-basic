"""Presenter-side logic: defaults for new sessions, applying to a running one, stop, alerts."""

import json

import pytest
from fastapi.testclient import TestClient
from support.demo import GatedSynth

from riva.audio.recordings import new_development_session
from riva.demo import presenter
from riva.demo.app import (
    MAX_MIC_LEVEL,
    MAX_PAUSE_MS,
    MIN_MIC_LEVEL,
    MIN_PAUSE_MS,
    build_demo_api,
    build_runtime,
)
from riva.demo.config import ConfigError, load_config
from riva.demo.fakes import FakeProbe, ScriptedTurnLLM, turn_json
from riva.demo.providers import Providers
from riva.demo.session import SessionSettings
from riva.experiments.vad_self_test import SyntheticWire


def make_runtime(tmp_path, replies=None):
    llm = ScriptedTurnLLM(replies if replies is not None else [turn_json()] * 6)
    synth = GatedSynth()
    synth.release.set()
    rt = build_runtime(
        load_config(), Providers(llm, synth, SyntheticWire), tmp_path / "archive", probe=FakeProbe()
    )
    return rt, llm


def answer(session, text="Hallo"):
    import numpy as np

    from riva.audio.recordings import RecordingStore

    store = RecordingStore(session.archive.directory.parents[1])
    saved = store.save((16000, np.zeros(16000, dtype=np.int16)), session.identity)
    return session.answer(text, saved, [text], [saved.metadata.turn_id], session.token())


def test_defaults_apply_to_new_sessions_only_and_are_validated(tmp_path) -> None:
    rt, _ = make_runtime(tmp_path)
    before = rt.ensure(new_development_session())
    rt.set_defaults(SessionSettings("buddy", "gpt-4.1", "marin"))
    after = rt.ensure(new_development_session())
    assert before.settings.persona_id == "expert" and after.settings.persona_id == "buddy"
    assert (after.settings.model, after.settings.voice) == ("gpt-4.1", "marin")
    invalid = (
        SessionSettings("x", "gpt-4.1", "marin"),
        SessionSettings("buddy", "gpt-9", "marin"),
        SessionSettings("buddy", "gpt-4.1", "robot"),
    )
    for bad in invalid:
        with pytest.raises(ConfigError):
            rt.set_defaults(bad)
    assert rt.defaults.persona_id == "buddy"  # a rejected choice changes nothing


def test_pause_threshold_is_bounded_and_served_to_the_page(tmp_path) -> None:
    rt, _ = make_runtime(tmp_path)
    client = TestClient(build_demo_api(rt))
    assert client.get("/riva/settings").json() == {"redemption_ms": 900, "mic_level": 0}
    assert rt.set_pause_ms(1500) == 1500
    assert client.get("/riva/settings").json() == {"redemption_ms": 1500, "mic_level": 0}
    for bad in (MIN_PAUSE_MS - 1, MAX_PAUSE_MS + 1):
        with pytest.raises(ValueError):
            rt.set_pause_ms(bad)
    assert rt.pause_ms == 1500


def test_mic_level_is_bounded_and_served_to_the_page(tmp_path) -> None:
    rt, _ = make_runtime(tmp_path)
    client = TestClient(build_demo_api(rt))
    assert client.get("/riva/settings").json()["mic_level"] == 0  # off by default
    assert rt.set_mic_level(30) == 30
    assert client.get("/riva/settings").json()["mic_level"] == 30
    for bad in (MIN_MIC_LEVEL - 1, MAX_MIC_LEVEL + 1):
        with pytest.raises(ValueError):
            rt.set_mic_level(bad)
    assert rt.mic_level == 30


def test_both_pages_are_served_on_one_server(tmp_path) -> None:
    rt, _ = make_runtime(tmp_path)
    client = TestClient(build_demo_api(rt), client=("127.0.0.1", 5000))
    participant, presenter_page = client.get("/"), client.get("/presenter/")
    assert participant.status_code == 200 and presenter_page.status_code == 200
    assert "Mein Tagebuch" in participant.text and "Vorführenden-Seite" in presenter_page.text
    assert (
        "Vorführenden-Seite" not in participant.text
    )  # nothing presenter-only on the participant page


def test_settings_can_be_applied_to_a_running_session(tmp_path) -> None:
    rt, llm = make_runtime(tmp_path)
    session = rt.ensure(new_define := new_development_session())
    answer(session)
    session.update_settings(SessionSettings("elder", "gpt-4.1", "sage"))
    answer(session, "Noch etwas.")
    assert llm.calls[0]["model"] == "gpt-4.1-mini" and llm.calls[1]["model"] == "gpt-4.1"
    assert rt.config.persona("elder").prompt in str(llm.calls[1]["instructions"])
    assert rt.config.core_prompt in str(llm.calls[1]["instructions"])  # core rules never change
    events = [
        json.loads(x)
        for x in (session.archive.directory / "timeline.jsonl").read_text().splitlines()
    ]
    assert [e for e in events if e["event"] == "settings_changed"][0]["persona"] == "elder"
    saved = json.loads((session.archive.directory / "session.json").read_text())
    assert saved["settings"]["persona"] == "elder" and saved["settings"]["voice"] == "sage"
    with pytest.raises(ConfigError):
        session.update_settings(SessionSettings("elder", "gpt-9", "sage"))
    assert new_define.session_id in rt.sessions


def test_stop_all_stops_every_active_session_and_signals_the_pages(tmp_path) -> None:
    rt, _ = make_runtime(tmp_path)
    a, b = rt.ensure(new_development_session()), rt.ensure(new_development_session())
    ra, rb = answer(a), answer(b)
    assert ra and rb
    assert (a.stop_seq, b.stop_seq) == (0, 0)
    assert rt.stop_all() == 2
    assert (a.stop_seq, b.stop_seq) == (1, 1) and a.view()["stop_seq"] == "1"
    assert a.phase == b.phase == "stopped" and a.epoch == b.epoch == 1
    b.close()
    assert rt.stop_all() == 1  # closed sessions are skipped


def test_the_presenter_sees_alerts_and_content_the_participant_page_does_not(tmp_path) -> None:
    distress = turn_json(
        flags=["possible_distress"], spoken_reply="Danke, dass du es mir erzählst."
    )
    rt, _ = make_runtime(tmp_path, [distress])
    session = rt.ensure(new_development_session())
    answer(session, "Mein Bruder hat mich geschlagen.")
    rows = presenter.session_rows(rt)
    assert rows[0][1] == "aktiv" and rows[0][-1] == 1 and rows[0][3] == "Experte"
    journal, heard, timeline, alerts = presenter.session_detail(rt, session.identity.session_id)
    assert "Mögliche Belastung" in alerts and "Ich war im Zoo." in journal
    assert any(e["event"] == "presenter_alert" for e in timeline)
    # The participant poll never carries alert information.
    assert "Belastung" not in json.dumps(session.view(), ensure_ascii=False)
    assert presenter.session_detail(rt, None)[3] == "Keine Sitzung ausgewählt."
    assert presenter.session_detail(rt, "nope")[3] == "Keine Sitzung ausgewählt."


def test_closed_sessions_stay_visible_with_their_reason(tmp_path) -> None:
    rt, _ = make_runtime(tmp_path)
    session = rt.ensure(new_development_session())
    rt.reset(session.identity.session_id)
    assert presenter.session_rows(rt)[0][1] == "beendet (presenter_reset)"
    assert presenter.session_choices(rt)[0][0].endswith("(beendet)")


def test_the_presenter_answers_loopback_clients_only(tmp_path) -> None:
    rt, _ = make_runtime(tmp_path)
    api = build_demo_api(rt)
    for host in ("127.0.0.1", "::1"):
        assert TestClient(api, client=(host, 5000)).get("/presenter/").status_code == 200
    for host in ("203.0.113.9", "192.168.1.20", "10.0.0.5", "2001:db8::1", "testclient"):
        remote = TestClient(api, client=(host, 5000))
        for method, path in (("get", "/presenter/"), ("get", "/presenter/gradio_api/info"),
                             ("post", "/presenter/gradio_api/queue/join")):  # fmt: skip
            response = getattr(remote, method)(path)
            assert response.status_code == 403 and "lokal" in response.text, (host, path)
    # the participant routes are not presenter routes
    remote = TestClient(api, client=("203.0.113.9", 5000))
    assert remote.get("/riva/settings").status_code == 200


def test_the_server_binds_to_loopback_and_offers_no_host_option(tmp_path, monkeypatch) -> None:
    import sys

    import uvicorn

    from riva.demo import app as demo_app

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda api, **kw: seen.update(kw))
    monkeypatch.setattr(sys, "argv", ["riva-demo", "--fake", "--private-dir", str(tmp_path / "p")])
    demo_app.main()
    assert seen["host"] == "127.0.0.1" == demo_app.HOST
    monkeypatch.setattr(sys, "argv", ["riva-demo", "--fake", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):  # there is deliberately no way to expose the demo from the CLI
        demo_app.main()
