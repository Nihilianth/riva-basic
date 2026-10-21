import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from riva.dev import archive_session as module


def test_command_output_preserves_large_exports() -> None:
    output = module.command_output(sys.executable, "-c", "print('x' * 1_000_000)")
    assert output == "x" * 1_000_000


def test_archive_keeps_export_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "git_value", lambda *a, **kw: None)
    monkeypatch.setattr(
        module,
        "command_output",
        lambda *a, **kw: (
            '{"session":{"id":"ses_test123"},"messages":[]}' if "api" in a else "v2.0.32"
        ),
    )
    archive = module.archive_session(
        "ses_test123",
        tmp_path / "private",
        discussion="docs/discussions/test.md",
        task_ids=["riva-test"],
    )
    exported = archive / "session-export.json"
    metadata = json.loads((archive / "metadata.json").read_text())
    assert metadata["sha256"] == hashlib.sha256(exported.read_bytes()).hexdigest()
    assert metadata["task_ids"] == ["riva-test"]
    assert exported.stat().st_mode & 0o777 == 0o600
    assert archive.stat().st_mode & 0o777 == 0o700


def test_destination_in_git_tree_is_rejected(tmp_path: Path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    with pytest.raises(ValueError, match="outside every Git"):
        module.archive_session("ses_test123", tmp_path / "ignored" / "private")


def test_invalid_id_is_rejected_before_export(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="session ID"):
        module.archive_session("../../other", tmp_path)


def test_invalid_export_creates_no_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "git_value", lambda *a, **kw: None)
    monkeypatch.setattr(module, "command_output", lambda *a, **kw: "not JSON")
    output = tmp_path / "private"
    with pytest.raises(ValueError):
        module.archive_session("ses_test123", output)
    assert not output.exists()
