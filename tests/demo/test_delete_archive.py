"""The documented procedure for deleting one complete session archive.

Covers scripts/delete_session_archive.py (dry run by default, closed sessions only).
"""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "delete_session_archive.py"
spec = importlib.util.spec_from_file_location("delete_session_archive", SCRIPT)
tool = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(tool)  # type: ignore[union-attr]


def make_session(root: Path, session_id: str, *, closed: bool = True) -> Path:
    session = root / "local-development" / session_id
    turn = session / "turnaaaa" / "replies" / "gen1"
    turn.mkdir(parents=True)
    (session / "turnaaaa" / "recording.wav").write_bytes(b"RIFF....")
    (session / "turnaaaa" / "transcript.json").write_text("{}")
    (turn / "speech.wav").write_bytes(b"RIFF....")
    (session / "timeline.jsonl").write_text('{"event": "x"}\n')
    (session / "journal.md").write_text("Ich war im Zoo.\n")
    (session / "utterances").mkdir()
    (session / "utterances" / "u1.json").write_text("{}")
    (session / "session.json").write_text(json.dumps({"closed_at": "t"} if closed else {}))
    return session


def test_a_dry_run_lists_everything_and_deletes_nothing(tmp_path, capsys) -> None:
    root = tmp_path / "demo-1"
    session = make_session(root, "a" * 32)
    assert tool.main([("a" * 32)[:8], "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "1 recordings" in out and "1 reply audio files" in out and "Dry run" in out
    assert session.is_dir()


def test_delete_removes_the_whole_session_including_recordings_and_replies(tmp_path) -> None:
    root = tmp_path / "demo-1"
    session = make_session(root, "b" * 32)
    other = make_session(root, "c" * 32)
    assert tool.main(["b" * 12, "--root", str(root), "--delete"]) == 0
    assert not session.exists()
    assert list(root.rglob("recording.wav")) == [other / "turnaaaa" / "recording.wav"]
    assert other.is_dir()  # a different session is never touched


def test_an_open_session_is_refused_unless_explicitly_allowed(tmp_path) -> None:
    root = tmp_path / "demo-1"
    session = make_session(root, "d" * 32, closed=False)
    with pytest.raises(SystemExit, match="not closed"):
        tool.main(["d" * 8, "--root", str(root), "--delete"])
    assert session.is_dir()
    assert tool.main(["d" * 8, "--root", str(root), "--delete", "--even-if-open"]) == 0
    assert not session.exists()


@pytest.mark.parametrize("bad", ["", "short", "../etc", "a/b" + "c" * 10, "x" * 65])
def test_malformed_ids_and_traversal_are_refused(tmp_path, bad) -> None:
    root = tmp_path / "demo-1"
    make_session(root, "e" * 32)
    with pytest.raises(SystemExit):
        tool.main([bad, "--root", str(root), "--delete"])
    assert (root / "local-development" / ("e" * 32)).is_dir()


def test_ambiguous_prefixes_unknown_ids_and_symlinks_are_refused(tmp_path) -> None:
    root = tmp_path / "demo-1"
    first = make_session(root, "ab12cd34" + "1" * 24)
    second = make_session(root, "ab12cd34" + "2" * 24)
    with pytest.raises(SystemExit, match="ambiguous"):
        tool.main(["ab12cd34", "--root", str(root), "--delete"])
    assert first.is_dir() and second.is_dir()
    with pytest.raises(SystemExit, match="No session"):
        tool.main(["9" * 12, "--root", str(root), "--delete"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    link = root / "local-development" / ("9" * 32)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SystemExit, match="not a plain directory"):
        tool.main(["9" * 12, "--root", str(root), "--delete"])
    assert (outside / "keep.txt").read_text() == "keep"
