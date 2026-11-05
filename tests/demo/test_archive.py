import json
import os
import stat
from pathlib import Path

import pytest

from riva.audio.recordings import SessionIdentity, new_development_session
from riva.demo.archive import SessionArchive

FIXED = "2026-11-05T10:00:00.000+00:00"


def archive(tmp_path: Path) -> SessionArchive:
    return SessionArchive(tmp_path / "root", new_development_session())


def test_layout_modes_and_timeline_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = archive(tmp_path)
    a.write_session(settings={"persona": "expert"})
    a.event("turn_started", utterance="u1", segments=["s1"])
    a.event("reply_started", generation="g1")
    lines = [json.loads(x) for x in (a.directory / "timeline.jsonl").read_text().splitlines()]
    assert [x["event"] for x in lines] == ["turn_started", "reply_started"]
    assert all(x["t"].endswith("+00:00") for x in lines)
    session = json.loads((a.directory / "session.json").read_text())
    assert session["settings"] == {"persona": "expert"} and session["schema_version"] == 1
    for name in ("session.json", "timeline.jsonl"):
        assert stat.S_IMODE(os.stat(a.directory / name).st_mode) == 0o600
    assert capsys.readouterr().out == ""  # content never goes to stdout/log


def test_session_json_merges_updates_and_utterances_and_journal_are_written(tmp_path: Path) -> None:
    a = archive(tmp_path)
    a.write_session(started_at="t0")
    a.write_session(closed_at="t1")
    assert json.loads((a.directory / "session.json").read_text())["started_at"] == "t0"
    rel = a.write_utterance(
        "abc123", {"verbatim_asr": ["Ähm ich war im Zoo"], "spoken_reply": "Schön."}
    )
    assert rel == "utterances/abc123.json"
    assert json.loads((a.directory / rel).read_text())["verbatim_asr"] == ["Ähm ich war im Zoo"]
    rel = a.write_journal(1, [{"id": "p0", "text": "Ich war im Zoo."}], "Ich war im Zoo.")
    assert (
        rel == "journal/0001.json"
        and (a.directory / "journal.md").read_text() == "Ich war im Zoo.\n"
    )
    with pytest.raises(ValueError):
        a.write_utterance("../escape", {})


def test_the_archive_refuses_a_location_inside_a_git_tree() -> None:
    inside_repo = Path(__file__).resolve().parents[2] / "should-not-exist"
    with pytest.raises(ValueError, match="outside every Git"):
        SessionArchive(inside_repo, SessionIdentity("u", "s"))
    assert not inside_repo.exists()


def test_sessions_do_not_share_files(tmp_path: Path) -> None:
    a, b = archive(tmp_path), archive(tmp_path)
    a.event("x")
    assert a.directory != b.directory and not (b.directory / "timeline.jsonl").exists()
