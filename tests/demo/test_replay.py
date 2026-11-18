"""Read-only session replay reader/renderer and the loopback-guarded audio routes (riva-skm.23).

All fixtures are synthetic and explicitly labelled; no real participant data is used.
"""

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from support.demo import GatedSynth
from support.replay_fixture import GEN, LLM_INPUT, PHRASES, SEG, write_session

from riva.demo import replay
from riva.demo.app import build_demo_api, build_runtime
from riva.demo.config import load_config
from riva.demo.fakes import FakeProbe, ScriptedTurnLLM, turn_json
from riva.demo.providers import Providers
from riva.experiments.vad_self_test import SyntheticWire


def make_runtime(tmp_path: Path) -> Any:
    synth = GatedSynth()
    synth.release.set()
    return build_runtime(
        load_config(),
        Providers(ScriptedTurnLLM([turn_json()] * 4), synth, SyntheticWire),
        tmp_path / "archive",
        probe=FakeProbe(),
    )


# --- the ordered model -----------------------------------------------------------------------


def test_load_replay_orders_steps_and_folds_turn_events(tmp_path: Path) -> None:
    user, sess = write_session(tmp_path)
    model = replay.load_replay(tmp_path, user, sess)
    assert model is not None
    # Folded events (llm_response/journal_applied/reply_started) are shown inside the turn block.
    assert [s.event for s in model.steps] == [
        "session_started", "segment_saved", "turn_started", "error_shown", "session_closed"
    ]  # fmt: skip
    assert model.summary.turns == 1 and model.summary.errors == 1


def test_turn_step_carries_llm_in_out_and_tts(tmp_path: Path) -> None:
    user, sess = write_session(tmp_path)
    model = replay.load_replay(tmp_path, user, sess)
    assert model is not None
    turn = next(s for s in model.steps if s.event == "turn_started")
    assert turn.llm_input == LLM_INPUT  # exactly what went into the LLM
    assert "spoken_reply" in turn.llm_output
    assert turn.reply.startswith("Schön")
    assert turn.tts_phrases == tuple(PHRASES)  # what was sent to TTS
    assert turn.reply_audio_gen == GEN  # speech.wav exists -> playable
    assert turn.outcome["grounding"] == 0.5
    segment = next(s for s in model.steps if s.event == "segment_saved")
    assert segment.verbatim == "Ich war im Zoo" and segment.segment_id == SEG


def test_zero_turn_session(tmp_path: Path) -> None:
    user, sess = write_session(tmp_path, with_turn=False)
    model = replay.load_replay(tmp_path, user, sess)
    assert model is not None
    assert [s.event for s in model.steps] == [
        "session_started", "segment_saved", "error_shown", "session_closed"
    ]  # fmt: skip
    assert model.summary.turns == 0
    replay.render_html(model)  # must not raise


def test_missing_utterance_renders_without_error(tmp_path: Path) -> None:
    user, sess = write_session(tmp_path, missing_utterance=True)
    model = replay.load_replay(tmp_path, user, sess)
    assert model is not None
    turn = next(s for s in model.steps if s.event == "turn_started")
    assert turn.missing  # no utterance json: surfaced, not crashed
    # the folded events are surfaced individually since their utterance is gone
    assert "reply_started" in [s.event for s in model.steps]
    assert "<script" not in replay.render_html(model)


def test_reply_without_audio_has_no_player(tmp_path: Path) -> None:
    user, sess = write_session(tmp_path, with_audio=False)
    model = replay.load_replay(tmp_path, user, sess)
    assert model is not None
    turn = next(s for s in model.steps if s.event == "turn_started")
    assert turn.reply_audio_gen is None and turn.reply_status == "failed"
    assert f"reply/{GEN}" not in replay.render_html(model)


# --- rendering and escaping ------------------------------------------------------------------


def test_render_html_escapes_content_and_links_audio(tmp_path: Path) -> None:
    user, sess = write_session(tmp_path, verbatim="<script>alert(1)</script>")
    model = replay.load_replay(tmp_path, user, sess)
    assert model is not None
    html = replay.render_html(model)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)" not in html  # the injected markup is inert
    assert f"/presenter/replay_audio/{user}/{sess}/segment/{SEG}" in html
    assert f"/presenter/replay_audio/{user}/{sess}/reply/{GEN}" in html
    assert "Eingabe an das Sprachmodell" in html  # the prompt is shown


def test_list_sessions_lists_all_on_disk_most_recent_first(tmp_path: Path) -> None:
    write_session(tmp_path, sess="aaaa1111synthetic0000000000000000")
    write_session(tmp_path, sess="bbbb2222synthetic0000000000000000", user="other-user")
    summaries = replay.list_sessions(tmp_path)
    assert len(summaries) == 2
    assert all("/" not in s.session_id for s in summaries)
    assert {s.user_id for s in summaries} == {"local-development", "other-user"}


# --- path / id validation --------------------------------------------------------------------


def test_path_validation_rejects_traversal_and_bad_ids(tmp_path: Path) -> None:
    user, sess = write_session(tmp_path)
    assert replay.session_dir(tmp_path, "../etc", sess) is None
    assert replay.session_dir(tmp_path, user, "..") is None
    assert replay.session_dir(tmp_path, user, "does-not-exist") is None
    assert replay.segment_audio_path(tmp_path, user, sess, "../../secret") is None
    assert replay.reply_audio_path(tmp_path, user, sess, "no-such-generation") is None
    assert replay.load_replay(tmp_path, "bad/../id", sess) is None
    # valid ids resolve to real private files
    assert replay.segment_audio_path(tmp_path, user, sess, SEG).is_file()  # type: ignore[union-attr]
    assert replay.reply_audio_path(tmp_path, user, sess, GEN).is_file()  # type: ignore[union-attr]


# --- the loopback-guarded audio routes -------------------------------------------------------


def test_replay_audio_routes_serve_locally_404_and_guard(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    user, sess = write_session(runtime.archive_root)
    api = build_demo_api(runtime)
    local = TestClient(api, client=("127.0.0.1", 11111))
    remote = TestClient(api, client=("203.0.113.9", 22222))

    seg = local.get(f"/presenter/replay_audio/{user}/{sess}/segment/{SEG}")
    assert seg.status_code == 200 and seg.headers["content-type"].startswith("audio/")
    rep = local.get(f"/presenter/replay_audio/{user}/{sess}/reply/{GEN}")
    assert rep.status_code == 200 and rep.content == b"RIFFsynthetic-reply-audio"

    # a valid-looking but unknown id is a clean 404 (not a shadowed Gradio page)
    missing = local.get(f"/presenter/replay_audio/{user}/{sess}/segment/nosuchsegment0000")
    assert missing.status_code == 404

    # defense in depth: a non-loopback client cannot reach anything under /presenter
    blocked = remote.get(f"/presenter/replay_audio/{user}/{sess}/segment/{SEG}")
    assert blocked.status_code == 403
