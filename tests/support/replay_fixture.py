"""Write a synthetic demo session archive on disk for replay tests (riva-skm.23).

All content here is synthetic and explicitly labelled; it is never real participant data.
The layout mirrors what riva.demo.archive / riva.audio.replies produce at runtime.
"""

import json
from pathlib import Path
from typing import Any

SEG = "seg1synthetic0000000000000000000"
GEN = "gen1synthetic0000000000000000000"
UTT = "utt1synthetic0000000000000000000"
LLM_INPUT = 'PROMPT · Neue Äußerung der Person:\n"Ich war im Zoo"'
PHRASES = ["Schön, dass du da warst!", "Was hast du im Zoo gesehen?"]
REPLY = "Schön, dass du da warst! Was hast du im Zoo gesehen?"


def write_session(
    root: Path,
    *,
    user: str = "local-development",
    sess: str = "sess1synthetic000000000000000000",
    verbatim: str = "Ich war im Zoo",
    with_turn: bool = True,
    with_audio: bool = True,
    missing_utterance: bool = False,
) -> tuple[str, str]:
    """Write one synthetic session under ``root/user/sess`` and return (user, sess)."""
    base = root / user / sess
    (base / SEG).mkdir(parents=True)
    (base / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "user_id": user,
                "session_id": sess,
                "started_at": "2026-11-05T10:00:00.000+00:00",
                "settings": {"persona": "expert", "llm_model": "gpt-4.1-mini", "voice": "ash"},
                "closed_at": "2026-11-05T10:05:00.000+00:00",
                "close_reason": "shutdown",
            }
        ),
        encoding="utf-8",
    )
    (base / SEG / "recording.wav").write_bytes(b"RIFFsynthetic-participant-audio")
    (base / SEG / "transcript.txt").write_text(verbatim, encoding="utf-8")

    if with_turn:
        reply_dir = base / SEG / "replies" / GEN
        reply_dir.mkdir(parents=True)
        reply_dir.joinpath("reply.json").write_text(
            json.dumps(
                {
                    "generation_id": GEN,
                    "status": "completed" if with_audio else "failed",
                    "requested_speech_phrases": PHRASES,
                    "config": {"voice": "ash", "tts_style": "ruhig"},
                }
            ),
            encoding="utf-8",
        )
        if with_audio:
            reply_dir.joinpath("speech.wav").write_bytes(b"RIFFsynthetic-reply-audio")

    if with_turn and not missing_utterance:
        (base / "utterances").mkdir()
        (base / "utterances" / f"{UTT}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "utterance_id": UTT,
                    "segments": [SEG],
                    "merged_transcript": verbatim,
                    "settings": {"persona": "expert", "llm_model": "gpt-4.1-mini", "voice": "ash"},
                    "llm_input": LLM_INPUT,
                    "llm_raw_outputs": ['{"spoken_reply":"Schön, dass du da warst!"}'],
                    "outcome": {
                        "source": "llm", "attempts": 1, "error_codes": [], "uncertain": True,
                        "soft_flags": ["low_grounding"], "flags": [], "grounding": 0.5,
                        "latency_ms": 1200, "tokens": [100, 20],
                    },
                    "journal_change": {
                        "kind": "appended", "passage": "p0", "previous_text": None, "reason": ""
                    },
                    "spoken_reply": REPLY,
                },
            ),
            encoding="utf-8",
        )  # fmt: skip

    events: list[dict[str, Any]] = [
        {"event": "session_started"},
        {"event": "segment_saved", "segment": SEG, "audio": f"{SEG}/recording.wav",
         "asr_status": "completed"},
    ]  # fmt: skip
    if with_turn:
        events += [
            {"event": "turn_started", "utterance": UTT, "segments": [SEG]},
            {"event": "llm_response", "utterance": UTT, "source": "llm", "latency_ms": 1200},
            {"event": "journal_applied", "utterance": UTT, "change": "appended", "passage": "p0"},
            {"event": "reply_started", "utterance": UTT, "generation": GEN},
        ]
    events += [
        {"event": "error_shown", "code": "asr_unusable"},
        {"event": "session_closed", "reason": "shutdown"},
    ]
    lines = "".join(
        json.dumps({"t": f"2026-11-05T10:00:{i:02d}.000+00:00", **event}) + "\n"
        for i, event in enumerate(events)
    )
    (base / "timeline.jsonl").write_text(lines, encoding="utf-8")
    return user, sess
