"""The content-bearing session archive: everything a later review player needs, private only.

Layout next to the segment recordings that ``RecordingStore`` already writes:

    <root>/<user_id>/<session_id>/
        session.json              settings snapshot, start/close times
        timeline.jsonl            one timestamped JSON event per line (relative file paths)
        journal.md                latest journal text; journal/<version>.json full history snapshots
        utterances/<id>.json      verbatim ASR texts, LLM input/output, outcome, reply
        <turn_id>/...             per-segment audio + transcript (written by RecordingStore)

Audio, transcripts, prompts and replies live ONLY here (private directory, outside every Git tree,
files 0600). The ordinary event log (``riva.event_log``) stays content-free by design.
"""

import json
import os
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from riva.audio.recordings import SessionIdentity, private_directory
from riva.audio.transcripts import atomic_private_text

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _now() -> datetime:
    return datetime.now(UTC)


class SessionArchive:
    def __init__(
        self,
        root: Path,
        identity: SessionIdentity,
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.identity, self.clock = identity, clock
        self.directory = private_directory(root / identity.user_id / identity.session_id)
        self._lock = threading.Lock()
        self._session: dict[str, Any] = {}

    def _stamp(self) -> str:
        return self.clock().isoformat(timespec="milliseconds")

    def relative(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.directory.resolve()))

    def write_session(self, **fields: Any) -> None:
        """Create or update ``session.json`` (fields are merged, the file replaced atomically)."""
        with self._lock:
            self._session.update(
                {"schema_version": 1, "user_id": self.identity.user_id,
                 "session_id": self.identity.session_id, **fields}
            )  # fmt: skip
            atomic_private_text(
                self.directory / "session.json",
                json.dumps(self._session, ensure_ascii=False, indent=2) + "\n",
            )

    def event(self, name: str, **fields: Any) -> None:
        """Append one timestamped event; values must be JSON-serializable."""
        line = json.dumps({"t": self._stamp(), "event": name, **fields}, ensure_ascii=False)
        with self._lock:
            fd = os.open(
                self.directory / "timeline.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    def write_utterance(self, utterance_id: str, record: dict[str, Any]) -> str:
        if not _ID.fullmatch(utterance_id):
            raise ValueError("Invalid utterance id")
        folder = private_directory(self.directory / "utterances")
        path = folder / f"{utterance_id}.json"
        atomic_private_text(
            path, json.dumps({"schema_version": 1, **record}, ensure_ascii=False, indent=2) + "\n"
        )
        return self.relative(path)

    def write_journal(self, version: int, passages: list[dict[str, Any]], text: str) -> str:
        folder = private_directory(self.directory / "journal")
        path = folder / f"{version:04d}.json"
        atomic_private_text(
            path,
            json.dumps({"version": version, "passages": passages}, ensure_ascii=False, indent=2)
            + "\n",
        )
        atomic_private_text(self.directory / "journal.md", text + "\n")
        return self.relative(path)
