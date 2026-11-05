"""Delete ONE complete Demo 1 session archive (recordings, transcripts, replies, journal, timeline).

Dry run by default: it only lists what would be removed. Nothing is deleted without --delete.

    uv run python scripts/delete_session_archive.py 3fa85f64              # list
    uv run python scripts/delete_session_archive.py 3fa85f64 --delete     # delete

The session id (or a unique prefix of at least 8 characters) is shown on the presenter page and in
`session.json`. Safety rules: the match must be a real directory (no symlinks) directly inside
`<root>/<user>/`, the session must have been closed (`closed_at` in session.json; reset it on the
presenter page or stop the demo first, or pass --even-if-open), and nothing outside the archive root
is ever touched. Voice previews (`demo-1-cache`) are not participant data and stay.
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

ID = re.compile(r"[A-Za-z0-9_-]{8,64}")


def default_root() -> Path:
    private = Path(os.environ.get("RIVA_PRIVATE_DIR", "../private"))
    return private.expanduser() / "research-data" / "demo-1"


def find_session(root: Path, session_id: str) -> Path:
    """The single session directory matching the id or prefix; raises SystemExit otherwise."""
    if not ID.fullmatch(session_id):
        raise SystemExit("Invalid session id (8-64 letters, digits, - or _).")
    if not root.is_dir() or root.is_symlink():
        raise SystemExit(f"Archive root not found: {root}")
    matches = [
        path
        for user in root.iterdir()
        if user.is_dir() and not user.is_symlink()
        for path in user.iterdir()
        if path.name.startswith(session_id)
    ]
    if not matches:
        raise SystemExit("No session archive matches that id.")
    if len(matches) > 1:
        raise SystemExit("The id is ambiguous; give more characters.")
    (match,) = matches
    if match.is_symlink() or not match.is_dir():
        raise SystemExit("Refusing: the match is not a plain directory.")
    resolved = match.resolve()
    if resolved.parent.parent != root.resolve():
        raise SystemExit("Refusing: the match is outside the archive root.")
    return match


def summarize(path: Path) -> dict[str, int]:
    files = [p for p in path.rglob("*") if p.is_file() or p.is_symlink()]
    return {
        "files": len(files),
        "recordings_wav": sum(1 for p in files if p.suffix == ".wav" and p.name == "recording.wav"),
        "reply_audio_wav": sum(1 for p in files if p.name == "speech.wav"),
        "bytes": sum(p.stat().st_size for p in files if p.is_file()),
    }


def is_closed(path: Path) -> bool:
    try:
        return "closed_at" in json.loads((path / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("session_id")
    parser.add_argument(
        "--root", type=Path, default=None, help="archive root (default: private dir)"
    )
    parser.add_argument("--delete", action="store_true", help="really delete (default: dry run)")
    parser.add_argument(
        "--even-if-open", action="store_true", help="allow a session not yet closed"
    )
    args = parser.parse_args(argv)
    root = (args.root or default_root()).expanduser()
    path = find_session(root, args.session_id)
    stats = summarize(path)
    print(f"Session archive: {path}")
    print(f"  {stats['files']} files, {stats['recordings_wav']} recordings, "
          f"{stats['reply_audio_wav']} reply audio files, {stats['bytes']} bytes")  # fmt: skip
    if not is_closed(path) and not args.even_if_open:
        raise SystemExit("Refusing: the session is not closed. Reset it first or stop the demo.")
    if not args.delete:
        print("Dry run - nothing deleted. Add --delete to remove it.")
        return 0
    shutil.rmtree(path)
    if path.exists():
        raise SystemExit("Deletion failed: the folder still exists.")
    print("Deleted. Ordinary logs never held this content; provider-side retention is separate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
