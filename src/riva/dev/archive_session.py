"""Archive an OpenCode V2 session privately, never inside a Git working tree."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def command_output(*args: str, cwd: Path | None = None) -> str:
    # Some CLI versions exit before flushing large JSON writes into a pipe. A
    # regular file avoids truncated session exports and stays private/unlinked.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
        subprocess.run(
            args,
            cwd=cwd,
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=120,
        )
        output.seek(0)
        return output.read().strip()


def git_value(*args: str, cwd: Path | None = None) -> str | None:
    try:
        return command_output("git", *args, cwd=cwd)
    except subprocess.CalledProcessError:
        return None


def ensure_private_destination(path: Path) -> None:
    """Reject tracked *and ignored* destinations inside any existing Git tree."""
    existing = path.resolve()
    while not existing.exists():
        existing = existing.parent
    if git_value("rev-parse", "--show-toplevel", cwd=existing):
        raise ValueError("Session archives must be outside every Git working tree")


def write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)


def archive_session(
    session_id: str,
    output_root: Path,
    *,
    discussion: str | None = None,
    task_ids: list[str] | None = None,
) -> Path:
    if not re.fullmatch(r"ses_[A-Za-z0-9]+", session_id):
        raise ValueError("Expected an OpenCode session ID starting with ses_")
    output_root = output_root.resolve()
    ensure_private_destination(output_root)
    # Collect and validate before creating an archive. Never print exported content.
    exported = command_output(
        "opencode", "api", "get", f"/api/experimental/session/{session_id}/export"
    )
    payload: Any = json.loads(exported)
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Unexpected OpenCode export response; expected a nonempty object")
    if "error" in payload:
        raise ValueError("OpenCode returned an error instead of a session export")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    now = datetime.now(UTC)
    metadata = {
        "session_id": session_id,
        "archived_at": now.isoformat(),
        "opencode_version": command_output("opencode", "--version"),
        "repository": git_value("rev-parse", "--show-toplevel"),
        "commit": git_value("rev-parse", "HEAD"),
        "working_tree_dirty": bool(git_value("status", "--porcelain")),
        "discussion": discussion,
        "task_ids": task_ids or [],
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "warning": "Private snapshot, not an environment backup; export API is experimental.",
    }
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = output_root / f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{session_id}"
    archive.mkdir(mode=0o700)
    write_private(archive / "session-export.json", serialized)
    write_private(archive / "metadata.json", json.dumps(metadata, indent=2) + "\n")
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="Private directory outside all Git checkouts",
    )
    parser.add_argument("--discussion", help="Path/reference to reviewed discussion notes")
    parser.add_argument("--task", action="append", default=[], dest="task_ids")
    args = parser.parse_args()
    try:
        archive = archive_session(
            args.session_id,
            args.output_root,
            discussion=args.discussion,
            task_ids=args.task_ids,
        )
    except (ValueError, OSError, subprocess.SubprocessError):
        # Export failures can contain private server responses; don't echo them.
        parser.exit(1, "Archive failed; check the session ID, service, and private destination.\n")
    print(f"Private session archive: {archive}")
