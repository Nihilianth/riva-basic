"""One complete interaction (speech clip -> ASR -> LLM -> TTS) with per-stage latency.

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


def measure_one_interaction(providers: Providers, archive_root: Path) -> dict[str, Any]:
    runtime = build_runtime(load_config(), providers, archive_root)
    identity = new_development_session()
    session = runtime.ensure(identity)
    payload, clip_ms = clip_payload()
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
    llm_and_commit_ms = (time.monotonic() - turn_started) * 1000
    for _ in report.run.updates():  # drains the audio queue like the page handler does
        pass
    run = report.run
    snapshot = run.snapshot()
    result = {
        "clip_ms": round(clip_ms),
        "asr": {
            "status": outcome.result.status,
            "total_ms": round(asr_ms),
            "audio_sent_ms": stages.get("asr.audio_sent"),
            "commit_sent_ms": stages.get("asr.commit_sent"),
            "completed_ms": stages.get("asr.completed"),
        },
        "llm": {"source": report.outcome.source, "attempts": report.outcome.attempts,
                "latency_ms": round(report.outcome.latency_ms),
                "tokens": [report.outcome.input_tokens, report.outcome.output_tokens]},
        "tts": {"first_audio_ms": snapshot["first_server_audio_ms"],
                "audio_seconds": snapshot["audio_seconds_produced"], "status": run.status},
        "turn_ms": round(llm_and_commit_ms),
        "journal_passages": len(session.journal.passages),
        "verbatim": session.verbatim,
        "journal": session.journal.text(),
        "reply": report.reply_text,
    }  # fmt: skip
    first_audio = snapshot["first_server_audio_ms"]
    # Time from the end of speech to the first reply audio being ready (before browser playback).
    result["end_of_speech_to_first_audio_ms"] = (
        None
        if first_audio is None
        else round(asr_ms + report.outcome.latency_ms + float(first_audio))
    )
    session.close("measurement_done")
    return result
