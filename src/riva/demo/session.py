"""One participant session: journal, history, safe reset and the commit gate.

A session lives from page load to reload/reset. Every asynchronous stage (ASR, LLM call, TTS
start) captures an ``EpochToken`` first and passes the commit gate - "this session is still the
current one" - under the session lock before any side effect (journal, archive, speech).
``close()`` flips the gate before it cancels work, so a result that arrives after a reset is
discarded (and audited in the OLD session's timeline), never applied to anything. Blocking
provider calls cannot be aborted; their threads finish and the results are dropped.
"""

import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from riva.audio.recordings import SavedRecording, SessionIdentity
from riva.audio.replies import ReplyConfig, ReplyManager, ReplyRun
from riva.demo.archive import SessionArchive
from riva.demo.classifier import Classifier
from riva.demo.config import DemoConfig
from riva.demo.journal import Journal, JournalChange
from riva.demo.prompts import TurnContext
from riva.demo.text import asr_looks_unreliable
from riva.demo.turn import TurnOutcome, TurnProcessor
from riva.experiments.asr_stream import (
    AsrSessions,
    SegmentOutcome,
    SegmentProgress,
    SegmentTimeline,
    gate_transcript,
    process_segment,
)


@dataclass(frozen=True)
class SessionSettings:
    """What the presenter chose for this session; snapshotted into ``session.json``."""

    persona_id: str
    model: str
    voice: str


@dataclass(frozen=True)
class EpochToken:
    epoch: int


@dataclass(frozen=True)
class TurnReport:
    utterance_id: str
    outcome: TurnOutcome
    change: JournalChange
    reply_text: str
    run: ReplyRun | None
    journal_text: str


class DemoSession:
    def __init__(
        self,
        *,
        identity: SessionIdentity,
        config: DemoConfig,
        settings: SessionSettings,
        processor: TurnProcessor,
        asr: AsrSessions,
        replies: ReplyManager,
        archive: SessionArchive,
        monotonic: Callable[[], float] = time.monotonic,
        classifier: Classifier | None = None,
        classifier_enabled: Callable[[], bool] = lambda: False,
        classifier_threshold: float = 0.75,
    ) -> None:
        config.check_choice(settings.persona_id, settings.model, settings.voice)
        self.identity, self.config, self.settings = identity, config, settings
        self.processor, self.asr, self.replies, self.archive = processor, asr, replies, archive
        self.monotonic = monotonic
        self.classifier = classifier
        self.classifier_enabled = classifier_enabled  # read live so the presenter toggle applies
        self.classifier_threshold = classifier_threshold
        self.created_at = monotonic()
        self.last_seen = self.created_at
        self.close_reason = ""
        self.lock = threading.RLock()
        self.epoch = 0
        self.closed = False
        self.journal = Journal(config.journal_tail)
        self.history: list[tuple[str, str]] = []
        self.used_words: list[str] = []
        self.last_reply_heard: bool | None = None
        self.last_run: ReplyRun | None = None
        self.verbatim: list[str] = []  # ASR text per saved segment, exactly as returned
        self.last_reply_text = ""
        self.stop_seq = 0  # bumps on every Stopp so the participant page stops its player at once
        self.phase = "idle"  # what the status indicator shows; see riva.demo.texts.PHASES
        self._error_until = 0.0
        self._pending: list[tuple[str, str]] = []  # (segment id, ASR text) held for the next turn
        self._journal_version = 0
        archive.write_session(
            started_at=archive._stamp(),
            settings={"persona": settings.persona_id, "llm_model": settings.model,
                      "voice": settings.voice, "tts_model": config.tts_model},
        )  # fmt: skip
        archive.event("session_started")

    # --- lifecycle ----------------------------------------------------------------------------

    def touch(self) -> None:
        """The page is alive (it polls every 300 ms); abandoned sessions stop being touched."""
        with self.lock:
            self.last_seen = self.monotonic()

    def idle_seconds(self) -> float:
        with self.lock:
            return self.monotonic() - self.last_seen

    # --- gate -------------------------------------------------------------------------------

    def token(self) -> EpochToken:
        with self.lock:
            return EpochToken(self.epoch)

    def is_current(self, token: EpochToken) -> bool:
        with self.lock:
            return not self.closed and token.epoch == self.epoch

    def _discard(self, stage: str, **fields: Any) -> None:
        self.archive.event("stale_discarded", stage=stage, **fields)

    def set_phase(self, phase: str, token: "EpochToken | None" = None) -> None:
        """Change what the indicator shows. With a token, a stale caller (a stage that was
        stopped or reset meanwhile) cannot overwrite the current state."""
        with self.lock:
            if self.closed or (token is not None and token.epoch != self.epoch):
                return
            self.phase = phase

    def resume(self) -> None:
        """The participant pressed Start again after a Stopp: listening state, nothing else."""
        with self.lock:
            if not self.closed and self.phase == "stopped":
                self.phase = "listening"

    def show_error(self, code: str, seconds: float = 6.0) -> None:
        """Show the friendly German error state for a while; ``code`` is a fixed identifier."""
        with self.lock:
            if self.closed:
                return
            self.phase = "error"
            self._error_until = self.monotonic() + seconds
        self.archive.event("error_shown", code=code)

    def view(self) -> dict[str, str]:
        """What the participant page shows (poll result); no technical detail."""
        with self.lock:
            if self.phase == "error" and self.monotonic() > self._error_until:
                self.phase = "listening"
            return {
                "phase": "closed" if self.closed else self.phase,
                "journal": self.journal.text(),
                "heard": "\n\n".join(self.verbatim),
                "reply": self.last_reply_text,
                "generation": self.last_run.generation_id if self.last_run else "",
                "session_id": self.identity.session_id,
                "stop_seq": str(self.stop_seq),
            }

    def _await_previous_turn(self, timeout: float = 10.0) -> None:
        """After a Stopp the interrupted utterance's ASR still finishes and is saved (audit trail);
        a new utterance waits for that instead of being rejected as "previous turn not saved"."""
        deadline = self.monotonic() + timeout
        while self.monotonic() < deadline:
            turn = self.asr.turns.get(self.identity.session_id)
            if turn is None or turn.persisted:
                return
            time.sleep(0.05)

    # --- ASR --------------------------------------------------------------------------------

    def segment_updates(
        self, payload: str, token: EpochToken
    ) -> Generator[SegmentProgress, None, SegmentOutcome | None]:
        """Run the shared segment pipeline; returns the outcome only if the session is current."""
        timeline = SegmentTimeline(self.asr, self.identity)
        self._await_previous_turn()
        try:
            outcome = yield from process_segment(payload, self.identity, self.asr, timeline)
        except KeyError:  # the ASR turn was released by a reset while we were waiting
            self._discard("asr")
            return None
        if outcome is None:
            return None
        if not self.is_current(token):
            self._discard("after_asr", segment=outcome.saved.metadata.turn_id)
            return None
        if outcome.result.status == "completed" and outcome.result.text:
            with self.lock:
                self.verbatim.append(outcome.result.text)
        self.archive.event(
            "segment_saved",
            segment=outcome.saved.metadata.turn_id,
            audio=self.archive.relative(outcome.saved.audio_path),
            transcript=self.archive.relative(outcome.saved.audio_path.parent / "transcript.json"),
            asr_status=outcome.result.status,
        )
        return outcome

    def on_segment(
        self, outcome: SegmentOutcome, token: EpochToken
    ) -> TurnReport | Literal["held", "unusable", "empty"] | None:
        """After ASR: hold the words while the user keeps talking, else answer everything together.

        Returns the report, ``"held"`` (waiting for more speech), ``"empty"`` (ASR completed but
        heard nothing - resume listening quietly), ``"unusable"`` (ASR failed - show the error) or
        ``None`` (the session was reset meanwhile).
        """
        text = outcome.result.text
        segment_id = outcome.saved.metadata.turn_id
        if outcome.result.status != "completed" or not text:
            self.archive.event("segment_unusable", segment=segment_id, asr=outcome.result.status)
            # Completed-but-empty means the VAD fired but nothing was said (silence, noise, the
            # loudspeaker): not an error, so resume listening quietly. A genuine ASR failure
            # (status != "completed") still surfaces the friendly error.
            return "empty" if outcome.result.status == "completed" else "unusable"
        if not self.is_current(token):
            self._discard("before_gate", segment=segment_id)
            return None
        decision = gate_transcript(
            self.asr, self.identity, text, outcome.segment_end_ms, outcome.saved
        )
        with self.lock:
            if self.closed or token.epoch != self.epoch:
                # Stopped or reset while the gate ran: whatever it held must not reach a later turn.
                self.asr.take_held(self.identity)
                self._discard("before_gate", segment=segment_id)
                return None
            if decision.hold:
                self._pending.append((segment_id, text))
                self.archive.event("turn_held", segment=segment_id)
                return "held"
            ids = [*(i for i, _ in self._pending), segment_id]
            texts = [*(t for _, t in self._pending), text]
            self._pending.clear()
            self.phase = "thinking"
        return self.answer(decision.merged, outcome.saved, texts, ids, token, outcome.started_ms)

    # --- one turn ---------------------------------------------------------------------------

    def _context(self, text: str, classified: str | None = None) -> TurnContext:
        return TurnContext(
            transcript=text,
            journal_story=self.journal.older_text(),  # whole story so far (read-only, no ids)
            journal_tail=self.journal.tail(),
            history=tuple(self.history[-self.config.history_exchanges :]),
            used_words=tuple(self.used_words),
            last_reply_heard=self.last_reply_heard,
            asr_uncertain=asr_looks_unreliable(text),
            classified=classified,
        )

    def _classify(self, text: str) -> str | None:
        """Optional Jev classification of the utterance (meta/correction/diary). Advisory only.

        Returns the label to hint the LLM with when confident, else None (fall back to prompt-only).
        Always records a ``classified`` event (label, probability, latency_ms) for profiling/review.
        """
        if self.classifier is None or not self.classifier_enabled():
            return None
        result = self.classifier.classify(text)
        if result is None:
            return None
        used = result.probability >= self.classifier_threshold
        self.archive.event(
            "classified",
            label=result.label,
            probability=round(result.probability, 3),
            latency_ms=round(result.latency_ms),
            threshold=self.classifier_threshold,
            used=used,
        )
        return result.label if used else None

    def _reply_config(self) -> ReplyConfig:
        persona = self.config.persona(self.settings.persona_id)
        return ReplyConfig(
            tts_model=self.config.tts_model,
            voice=self.settings.voice,
            tts_style=persona.tts_style,
        )

    def answer(
        self,
        merged_text: str,
        saved: SavedRecording,
        asr_texts: list[str],
        segment_ids: list[str],
        token: EpochToken,
        client_started_ms: float = 0.0,
    ) -> TurnReport | None:
        """LLM turn, journal and spoken reply for one merged utterance (blocks during the LLM)."""
        utterance_id = uuid4().hex
        classified = self._classify(merged_text)  # network call: outside the lock
        with self.lock:
            if self.closed or token.epoch != self.epoch:
                self._discard("before_llm", utterance=utterance_id)
                return None
            context = self._context(merged_text, classified)
            revisable = self.journal.revisable_ids()
            settings = self.settings
        self.archive.event("turn_started", utterance=utterance_id, segments=segment_ids)
        outcome = self.processor.process(
            context, persona_id=settings.persona_id, model=settings.model, revisable_ids=revisable
        )
        self.archive.event(
            "llm_response",
            utterance=utterance_id,
            source=outcome.source,
            attempts=outcome.attempts,
            error_codes=list(outcome.error_codes),
            latency_ms=round(outcome.latency_ms),
            tokens=[outcome.input_tokens, outcome.output_tokens],
        )
        with self.lock:
            if self.closed or token.epoch != self.epoch:
                self._discard("after_llm", utterance=utterance_id)
                return None
            return self._commit(
                utterance_id, outcome, merged_text, saved, asr_texts, segment_ids, client_started_ms
            )

    def _commit(
        self,
        utterance_id: str,
        outcome: TurnOutcome,
        merged_text: str,
        saved: SavedRecording,
        asr_texts: list[str],
        segment_ids: list[str],
        client_started_ms: float,
    ) -> TurnReport:
        """Apply the turn. Caller holds the lock and has passed the commit gate."""
        change = self.journal.apply(outcome, saved.metadata.turn_id, utterance_id, merged_text)
        reply_text = change.reply_override or outcome.spoken_reply
        self.last_reply_text = reply_text
        self.history.append((merged_text, reply_text))
        del self.history[: -self.config.history_exchanges]
        if outcome.result and outcome.result.vocabulary:
            self.used_words.append(outcome.result.vocabulary[0])
        self.last_reply_heard = None  # the new reply has not been heard yet
        self._journal_version += 1
        journal_path = self.archive.write_journal(
            self._journal_version, self.journal.snapshot(), self.journal.text()
        )
        self.archive.write_utterance(
            utterance_id,
            {
                "utterance_id": utterance_id,
                "segments": segment_ids,
                "verbatim_asr": asr_texts,  # exactly what ASR returned, per segment
                "merged_transcript": merged_text,
                "settings": {"persona": self.settings.persona_id, "llm_model": self.settings.model,
                             "voice": self.settings.voice},
                "llm_input": outcome.user_input,
                "llm_raw_outputs": list(outcome.raw_outputs),
                "outcome": {
                    "source": outcome.source,
                    "attempts": outcome.attempts,
                    "error_codes": list(outcome.error_codes),
                    "uncertain": outcome.uncertain,
                    "soft_flags": list(outcome.validated.soft_flags) if outcome.validated else [],
                    "flags": list(outcome.flags),
                    "grounding": outcome.validated.grounding if outcome.validated else None,
                    "latency_ms": round(outcome.latency_ms),
                    "tokens": [outcome.input_tokens, outcome.output_tokens],
                },
                "journal_change": {"kind": change.kind, "passage": change.passage_id,
                                   "previous_text": change.previous_text, "reason": change.reason},
                "journal_version": self._journal_version,
                "spoken_reply": reply_text,
            },
        )  # fmt: skip
        self.archive.event(
            "journal_applied",
            utterance=utterance_id,
            change=change.kind,
            passage=change.passage_id,
            journal=journal_path,
        )
        if change.reason.startswith("revise_"):
            # A doubtful correction left the journal untouched; the utterance is in the archive.
            self.archive.event(
                "revise_blocked", utterance=utterance_id, reason=change.reason, passage=None
            )
        if change.kind == "revised":
            self.archive.event("passage_revised", utterance=utterance_id, passage=change.passage_id)
        if "possible_distress" in outcome.flags:
            self.archive.event("presenter_alert", utterance=utterance_id, kind="possible_distress")
        run: ReplyRun | None = None
        try:
            run, _ = self.replies.start(
                self.identity, saved, reply_text, client_started_ms, self._reply_config()
            )
            self.last_run = run
            self.archive.event(
                "reply_started", utterance=utterance_id, generation=run.generation_id
            )
        except (ValueError, OSError):
            self.archive.event("reply_failed", utterance=utterance_id, code="reply_start_failed")
        return TurnReport(utterance_id, outcome, change, reply_text, run, self.journal.text())

    # --- speech control ---------------------------------------------------------------------

    def update_settings(self, settings: SessionSettings) -> None:
        """Presenter changed persona/model/voice for this running session (validated, audited)."""
        self.config.check_choice(settings.persona_id, settings.model, settings.voice)
        with self.lock:
            if self.closed:
                return
            self.settings = settings
        self.archive.event(
            "settings_changed",
            persona=settings.persona_id,
            llm_model=settings.model,
            voice=settings.voice,
        )
        self.archive.write_session(
            settings={"persona": settings.persona_id, "llm_model": settings.model,
                      "voice": settings.voice, "tts_model": self.config.tts_model}
        )  # fmt: skip

    def interrupt(self, reason: str = "stopped") -> bool:
        """Stopp: end the current interaction wherever it is, without closing the session.

        It reuses the epoch/commit gate: bumping the epoch makes every result of a stage that is
        still running (ASR, LLM call, TTS start) stale, so it is discarded and audited instead of
        being applied. Held fragments are dropped, a reply being synthesized or played is cancelled
        (and serves no more audio). Journal entries that were already committed stay.
        """
        with self.lock:
            if self.closed:
                return False
            self.epoch += 1
            self.stop_seq += 1
            self._pending.clear()
            self.phase = "stopped"
            run = self.last_run
            if run is not None:
                self._mark_unheard_if_audible(run)
        self.asr.take_held(self.identity)
        stopped = self.replies.stop(self.identity) if run is not None else None
        self.archive.event(
            "interrupted",
            reason=reason,
            generation=stopped.generation_id if stopped else None,
            heard_ms=stopped.snapshot().get("playback_duration_ms") if stopped else None,
        )
        return True

    def _mark_unheard_if_audible(self, run: ReplyRun) -> None:
        """Caller holds the lock. A reply still being produced or still playing counts as not fully
        heard, so an unheard follow-up question may be asked again; an old one counts as heard."""
        if self.last_reply_heard is not None:
            return
        snapshot = run.snapshot()
        audio_s = float(snapshot.get("audio_seconds_produced") or 0)  # type: ignore[arg-type]
        playing_until = run.started + (run.first_audio_ms or 0) / 1000 + audio_s
        audible = run.status in ("generating", "cancelled") or self.monotonic() < playing_until
        self.last_reply_heard = not audible
        if audible:
            self.archive.event("reply_interrupted", generation=run.generation_id)

    def note_user_speech_started(self) -> None:
        """Barge-in: if the user starts speaking while the reply should still be playing, the last
        reply counts as not fully heard (so an unheard follow-up question may be asked again)."""
        with self.lock:
            run = self.last_run
            if self.closed or run is None:
                return
            self._mark_unheard_if_audible(run)

    # --- reset ------------------------------------------------------------------------------

    def close(self, reason: str = "reset") -> None:
        """End the session. Idempotent. In-flight work is cancelled; late results are discarded."""
        with self.lock:
            if self.closed:
                return
            self.closed = True  # the gate closes first, then work is cancelled
            self.close_reason = reason
            self.epoch += 1
            self._pending.clear()
        self.replies.release(self.identity)
        self.asr.release(self.identity)
        self.archive.event("session_closed", reason=reason)
        self.archive.write_session(closed_at=self.archive._stamp(), close_reason=reason)
