"""Complete interactions (clip -> ASR -> LLM -> TTS) with per-stage latency and a budget guard.

The same code runs offline with fake providers (tests/demo/test_e2e_measure.py) and, only after the
user's approval of the paid calls, against the real services (tests/demo/live/test_e2e_smoke.py).
Browser capture and playback are not part of this measurement.
"""

import base64
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from riva.audio.recordings import new_development_session
from riva.demo.app import build_runtime
from riva.demo.config import load_config
from riva.demo.providers import Providers
from support.audio import clip

CLIP = "zoo"
CONVERSATION = ("zoo", "elefanten")  # turn 2 answers the follow-up question of turn 1

# Hard limits that bound the cost of a live run by construction (prices below are approximate).
MAX_ASR_SESSIONS = 3
MAX_LLM_CALLS = 6
MAX_TTS_PHRASES = 12
MAX_TTS_SECONDS = 90.0
PRICE_LLM_USD_PER_M = (0.40, 1.60)  # gpt-4.1-mini input/output tokens (approximate)
PRICE_TTS_USD_PER_MIN = 0.015  # gpt-4o-mini-tts audio output (approximate)
PRICE_ASR_USD_PER_MIN = 0.02  # deliberately generous upper bound for realtime transcription


class BudgetStop(BaseException):
    """Not an Exception on purpose: nothing in the pipeline may swallow it."""


class Budget:
    def __init__(self) -> None:
        self.asr_sessions = self.llm_calls = self.tts_phrases = 0
        self.tokens_in = self.tokens_out = 0
        self.tts_seconds = self.asr_seconds = 0.0

    def estimate_usd(self) -> float:
        llm = (
            self.tokens_in * PRICE_LLM_USD_PER_M[0] + self.tokens_out * PRICE_LLM_USD_PER_M[1]
        ) / 1e6
        return (
            llm
            + self.tts_seconds / 60 * PRICE_TTS_USD_PER_MIN
            + self.asr_seconds / 60 * PRICE_ASR_USD_PER_MIN
        )

    def worst_case_usd(self) -> float:
        """Upper bound if every limit were reached (what the guard allows, not what is expected)."""
        llm = MAX_LLM_CALLS * (3000 * PRICE_LLM_USD_PER_M[0] + 700 * PRICE_LLM_USD_PER_M[1]) / 1e6
        return (
            llm
            + MAX_TTS_SECONDS / 60 * PRICE_TTS_USD_PER_MIN
            + MAX_ASR_SESSIONS * 0.5 * PRICE_ASR_USD_PER_MIN
        )


def guarded(providers: Providers, budget: Budget) -> Providers:
    """Wrap real providers so that no limit above can be exceeded."""
    from riva.demo.turn import LLMReply

    class LLM:
        def respond(self, instructions: str, user_input: str, **kwargs: Any) -> LLMReply:
            if budget.llm_calls >= MAX_LLM_CALLS:
                raise BudgetStop("llm call limit")
            budget.llm_calls += 1
            reply = providers.llm.respond(instructions, user_input, **kwargs)
            budget.tokens_in += reply.input_tokens
            budget.tokens_out += reply.output_tokens
            return reply

    class Speech:
        def speech(self, phrase: str, config: Any) -> Any:
            if budget.tts_phrases >= MAX_TTS_PHRASES or budget.tts_seconds >= MAX_TTS_SECONDS:
                raise BudgetStop("tts limit")
            budget.tts_phrases += 1
            for chunk in providers.speech.speech(phrase, config):
                budget.tts_seconds += len(chunk) / 2 / 24000
                yield chunk

    def wire() -> Any:
        if budget.asr_sessions >= MAX_ASR_SESSIONS:
            raise BudgetStop("asr session limit")
        budget.asr_sessions += 1
        return providers.wire_factory()

    return Providers(LLM(), Speech(), wire)  # type: ignore[arg-type]


def clip_payload(name: str = CLIP) -> tuple[str, float]:
    """The clip as the browser would submit it, and its duration in milliseconds."""
    samples = clip(name)
    pcm = np.clip(np.rint(samples * 32767), -32768, 32767).astype("<i2").tobytes()
    duration_ms = len(samples) / 16.0
    payload = json.dumps(
        {
            "pcm16_b64": base64.b64encode(pcm).decode("ascii"),
            "started_ms": 0.0,
            "events": [{"stage": "vad.speech_end", "at_ms": duration_ms}],
        }
    )
    return payload, duration_ms


def _one_turn(
    runtime: Any, session: Any, identity: Any, clip_name: str, budget: Budget
) -> dict[str, Any]:
    payload, clip_ms = clip_payload(clip_name)
    budget.asr_seconds += clip_ms / 1000
    token = session.token()
    started = time.monotonic()
    segment = session.segment_updates(payload, token)
    try:
        while True:
            next(segment)
    except StopIteration as stop:
        outcome = stop.value
    asr_ms = (time.monotonic() - started) * 1000
    assert outcome is not None, "the segment was rejected"
    stages = {row["stage"]: row["elapsed_ms"] for row in runtime.asr.get_vad_diagnostics(identity)}
    turn_started = time.monotonic()
    report = session.on_segment(outcome, token)
    assert report not in (None, "held", "unusable"), report
    turn_ms = (time.monotonic() - turn_started) * 1000
    for _ in report.run.updates():  # drains the audio queue like the page handler does
        pass
    run = report.run
    snapshot = run.snapshot()
    first_audio = snapshot["first_server_audio_ms"]
    return {
        "clip": clip_name,
        "clip_ms": round(clip_ms),
        "asr": {
            "status": outcome.result.status,
            "total_ms": round(asr_ms),
            "audio_sent_ms": stages.get("asr.audio_sent"),
            "commit_sent_ms": stages.get("asr.commit_sent"),
            "completed_ms": stages.get("asr.completed"),
        },
        "llm": {
            "source": report.outcome.source,
            "attempts": report.outcome.attempts,
            "error_codes": list(report.outcome.error_codes),
            "latency_ms": round(report.outcome.latency_ms),
            "tokens": [report.outcome.input_tokens, report.outcome.output_tokens],
            "uncertain": report.outcome.uncertain,
        },
        "tts": {
            "first_audio_ms": first_audio,
            "audio_seconds": snapshot["audio_seconds_produced"],
            "status": run.status,
            "error_code": snapshot["error_code"],
        },
        "turn_ms": round(turn_ms),
        "end_of_speech_to_first_audio_ms": None
        if first_audio is None
        else round(asr_ms + report.outcome.latency_ms + float(first_audio)),
        "asr_text": session.verbatim[-1] if session.verbatim else "",
        "passage": session.journal.passages[-1].text if session.journal.passages else "",
        "change": report.change.kind,
        "reply": report.reply_text,
        "reply_has_question": "?" in report.reply_text,
    }


def measure_conversation(
    providers: Providers,
    archive_root: Path,
    clips: tuple[str, ...] = CONVERSATION,
    budget: Budget | None = None,
) -> dict[str, Any]:
    budget = budget or Budget()
    runtime = build_runtime(load_config(), guarded(providers, budget), archive_root)
    identity = new_development_session()
    session = runtime.ensure(identity)
    turns = []
    try:
        for name in clips:
            turns.append(_one_turn(runtime, session, identity, name, budget))
        result: dict[str, Any] = {"turns": turns, "journal": session.journal.text(),
                                  "verbatim": list(session.verbatim)}  # fmt: skip
    finally:
        session.close("measurement_done")
    result["budget"] = {
        "asr_sessions": budget.asr_sessions, "llm_calls": budget.llm_calls,
        "tts_phrases": budget.tts_phrases, "tts_seconds": round(budget.tts_seconds, 1),
        "asr_audio_seconds": round(budget.asr_seconds, 1),
        "tokens": [budget.tokens_in, budget.tokens_out],
        "estimated_usd": round(budget.estimate_usd(), 4),
        "guard_worst_case_usd": round(budget.worst_case_usd(), 3),
    }  # fmt: skip
    return result


def measure_one_interaction(providers: Providers, archive_root: Path) -> dict[str, Any]:
    """A single isolated utterance (kept for the offline check)."""
    result = measure_conversation(providers, archive_root, clips=(CLIP,))
    turn = result["turns"][0]
    return {
        **turn,
        "journal_passages": 1,
        "verbatim": result["verbatim"],
        "journal": result["journal"],
    }
