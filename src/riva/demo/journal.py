"""The cumulative journal: append-only passages, controlled revisions, full history kept.

Conservative by design: an ambiguous or doubtful correction never edits a passage. The
``low_grounding`` flag that blocks a revision is a WARNING heuristic (word overlap between the new
text and what was said), not a check that a correction is semantically right; it can be wrong in
both directions, which is why it only ever makes the journal MORE cautious.

The LLM only ever writes the passage for the current utterance (``append``) or, on an explicit and
unambiguous user correction, a new text for one of the last few passages (``revise``). The app
applies these deterministically; nothing else in the journal can change.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from riva.demo.turn import TurnOutcome

CLARIFY_REPLY = "Welchen Teil meinst du? Sag mir bitte noch einmal, was ich ändern soll."


@dataclass
class Revision:
    """One change to a passage. The original text and the utterance that caused it are kept."""

    previous_text: str
    turn_id: str
    reason: str
    utterance_id: str = ""
    utterance_text: str = ""  # what the participant said (merged ASR text) - content, private


@dataclass
class Passage:
    id: str
    text: str
    turn_id: str
    source: Literal["llm", "verbatim_fallback"]
    uncertain: bool
    revisions: list[Revision] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "turn_id": self.turn_id,
            "source": self.source,
            "uncertain": self.uncertain,
            "revisions": [
                {
                    "previous_text": r.previous_text,
                    "turn_id": r.turn_id,
                    "reason": r.reason,
                    "utterance_id": r.utterance_id,
                    "utterance_text": r.utterance_text,
                }
                for r in self.revisions
            ],
        }


@dataclass(frozen=True)
class JournalChange:
    kind: Literal["appended", "revised", "unchanged"]
    passage_id: str | None = None
    previous_text: str | None = None
    reason: str = ""
    reply_override: str | None = None  # a clarification to speak instead of the model's reply


class Journal:
    def __init__(self, window: int = 5) -> None:
        self.passages: list[Passage] = []
        self.window = window
        self._next = 0

    def tail(self) -> tuple[tuple[str, str], ...]:
        """The latest passages (id, text) the LLM may see and revise, oldest first."""
        return tuple((p.id, p.text) for p in self.passages[-self.window :])

    def revisable_ids(self) -> frozenset[str]:
        return frozenset(pid for pid, _ in self.tail())

    def older_text(self) -> str:
        """The entries BEFORE the revisable tail, as plain first-person text (no ids).

        Shown to the LLM as the read-only "story so far" so it has the whole journal for continuity
        without revising an old entry (only ``tail`` entries carry ids and are revisable).
        """
        older = self.passages[: -self.window] if len(self.passages) > self.window else []
        return "\n\n".join(p.text for p in older)

    def text(self) -> str:
        """The journal as one document: a paragraph per passage."""
        return "\n\n".join(p.text for p in self.passages)

    def snapshot(self) -> list[dict[str, Any]]:
        return [p.snapshot() for p in self.passages]

    def _append(
        self, text: str, turn_id: str, source: Literal["llm", "verbatim_fallback"], uncertain: bool
    ) -> Passage:
        passage = Passage(f"p{self._next}", text.strip(), turn_id, source, uncertain)
        self._next += 1
        self.passages.append(passage)
        return passage

    def apply(
        self,
        outcome: TurnOutcome,
        turn_id: str,
        utterance_id: str = "",
        utterance_text: str = "",
    ) -> JournalChange:
        op = outcome.journal_op
        if outcome.source == "fallback":
            passage = self._append(op.text, turn_id, "verbatim_fallback", True)
            return JournalChange("appended", passage.id, reason="fallback_verbatim")
        if op.type == "append":
            passage = self._append(op.text, turn_id, "llm", outcome.uncertain)
            return JournalChange("appended", passage.id)
        if op.type == "none":
            return JournalChange("unchanged", reason="no_change")
        return self._revise(outcome, turn_id, utterance_id, utterance_text)

    def _revise(
        self, outcome: TurnOutcome, turn_id: str, utterance_id: str, utterance_text: str
    ) -> JournalChange:
        op = outcome.journal_op
        target = next((p for p in self.passages[-self.window :] if p.id == op.target_id), None)
        result = outcome.validated.result if outcome.validated else None
        soft = outcome.validated.soft_flags if outcome.validated else ()
        # Block only a genuinely doubtful correction: unknown target, the model itself unsure, or
        # invented facts. Low grounding of the revised text is EXPECTED for a correction (it
        # restates the entry, not the short correction utterance), so it must not block -
        # validate_turn already requires the correction's evidence to be grounded in what was said
        # (grounding is a soft signal, never a gate).
        doubtful = (
            result is None
            or result.uncertain
            or result.clarification_needed
            or "added_facts" in soft
        )
        if target is None or doubtful:
            # Doubtful correction: never guess. Keep the journal, ask instead.
            reply = outcome.spoken_reply
            return JournalChange(
                "unchanged",
                reason="revise_blocked_uncertain" if target else "revise_target_missing",
                reply_override=None if "?" in reply else CLARIFY_REPLY,
            )
        target.revisions.append(
            Revision(target.text, turn_id, "user_correction", utterance_id, utterance_text)
        )
        target.text = op.text.strip()
        return JournalChange("revised", target.id, target.revisions[-1].previous_text)
