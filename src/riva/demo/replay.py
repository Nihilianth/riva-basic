"""Read-only reconstruction of an archived demo session for the presenter review tab.

Reads the private session archive written by :mod:`riva.demo.archive` and
:mod:`riva.audio.replies` and returns an ordered model (and an HTML rendering) of one
session: participant segments, LLM turns (the prompt that went in, the raw output, the
metrics), replies (what was sent to TTS plus the synthesized audio) and control events,
all in ``timeline.jsonl`` order. See ``docs/demo/architecture.md``.

Guarantees: this module only reads. It never writes, never resolves a path outside the
archive root (ids are validated and resolved paths are checked against the root), and
never emits archive content to logs. The content it returns is private participant data
and is shown only on the loopback-guarded presenter page.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeGuard

# Same shape the archive uses for user/session/segment/generation ids.
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Where the presenter page fetches replay audio; the matching routes live in app.py and
# are inside the LocalOnlyPresenter loopback guard because the path starts with /presenter.
AUDIO_BASE = "/presenter/replay_audio"

# Timeline events folded into their turn block (they all belong to one utterance) rather
# than shown as separate lines. Surfaced individually only if their utterance is missing.
_FOLDED = frozenset(
    {"llm_response", "journal_applied", "reply_started", "passage_revised", "revise_blocked"}
)

_EVENT_LABELS: dict[str, tuple[str, str]] = {
    "session_started": ("▶", "Sitzung gestartet"),
    "session_closed": ("■", "Sitzung beendet"),
    "settings_changed": ("⚙", "Einstellungen geändert"),
    "turn_held": ("⏸", "Segment zurückgehalten (wartet auf Fortsetzung)"),
    "segment_unusable": ("✕", "Segment unbrauchbar"),
    "error_shown": ("⚠", "Fehler angezeigt"),
    "reply_interrupted": ("⏹", "Antwort unterbrochen (nicht ganz gehört)"),
    "interrupted": ("⏹", "Unterbrochen"),
    "stale_discarded": ("↯", "Verworfenes Ergebnis"),
    "presenter_alert": ("⚠", "Hinweis"),
    "llm_response": ("…", "Sprachmodell geantwortet"),
    "journal_applied": ("📓", "Tagebuch geändert"),
    "reply_started": ("🔊", "Antwort gestartet"),
    "passage_revised": ("✎", "Eintrag überarbeitet"),
    "revise_blocked": ("∅", "Überarbeitung blockiert"),
}


def _is_id(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_timeline(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def session_dir(archive_root: Path, user_id: str, session_id: str) -> Path | None:
    """The validated session directory, or None if the ids are bad or escape the root."""
    if not (_is_id(user_id) and _is_id(session_id)):
        return None
    root = archive_root.resolve()
    candidate = (root / user_id / session_id).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_dir():
        return None
    return candidate


def segment_audio_path(
    archive_root: Path, user_id: str, session_id: str, segment_id: str
) -> Path | None:
    """Private ``<segment>/recording.wav`` for the review player, fully validated."""
    directory = session_dir(archive_root, user_id, session_id)
    if directory is None or not _is_id(segment_id):
        return None
    path = (directory / segment_id / "recording.wav").resolve()
    if path.is_relative_to(directory) and path.is_file():
        return path
    return None


def reply_audio_path(
    archive_root: Path, user_id: str, session_id: str, generation_id: str
) -> Path | None:
    """Private synthesized ``speech.wav`` for a generation, found within the session dir."""
    directory = session_dir(archive_root, user_id, session_id)
    if directory is None or not _is_id(generation_id):
        return None
    for found in directory.glob(f"*/replies/{generation_id}/speech.wav"):
        path = found.resolve()
        if path.is_relative_to(directory) and path.is_file():
            return path
    return None


@dataclass(frozen=True)
class SessionSummary:
    user_id: str
    session_id: str
    started_at: str
    closed_at: str | None
    close_reason: str | None
    persona: str
    model: str
    voice: str
    turns: int
    alerts: int
    errors: int

    @property
    def value(self) -> str:
        """Dropdown value; ``user_id`` and ``session_id`` are validated ids (no slash)."""
        return f"{self.user_id}/{self.session_id}"

    @property
    def label(self) -> str:
        when = self.started_at[:19].replace("T", " ") or "?"
        state = "beendet" if self.closed_at else "aktiv"
        return f"{when} · {self.session_id[:8]} · {state} · {self.turns} Antw."


@dataclass(frozen=True)
class Step:
    """One rendered item in session order: a participant segment, an LLM turn, or an event."""

    t: str
    event: str
    detail: str = ""
    # participant segment
    verbatim: str = ""
    asr_status: str = ""
    segment_id: str | None = None
    # LLM turn
    merged_transcript: str = ""
    llm_input: str = ""
    llm_output: str = ""
    outcome: dict[str, Any] = field(default_factory=dict)
    journal_change: dict[str, Any] = field(default_factory=dict)
    reply: str = ""
    voice: str = ""
    tts_phrases: tuple[str, ...] = ()
    reply_status: str = ""
    reply_audio_gen: str | None = None
    missing: bool = False


@dataclass(frozen=True)
class SessionReplay:
    summary: SessionSummary
    steps: tuple[Step, ...]


def _hms(value: Any) -> str:
    return value[11:19] if isinstance(value, str) and len(value) >= 19 and "T" in value else ""


def _summary(
    user_id: str, session_id: str, session: dict[str, Any], events: list[str]
) -> SessionSummary:
    raw_settings = session.get("settings")
    settings = raw_settings if isinstance(raw_settings, dict) else {}
    return SessionSummary(
        user_id=user_id,
        session_id=session_id,
        started_at=str(session.get("started_at") or ""),
        closed_at=session.get("closed_at") if isinstance(session.get("closed_at"), str) else None,
        close_reason=(
            session.get("close_reason") if isinstance(session.get("close_reason"), str) else None
        ),
        persona=str(settings.get("persona") or "?"),
        model=str(settings.get("llm_model") or "?"),
        voice=str(settings.get("voice") or "?"),
        turns=events.count("turn_started"),
        alerts=events.count("presenter_alert"),
        errors=events.count("error_shown"),
    )


def list_sessions(archive_root: Path) -> list[SessionSummary]:
    """Every session found on disk under the archive root, most recent first."""
    root = archive_root.resolve()
    if not root.is_dir():
        return []
    summaries: list[SessionSummary] = []
    for user_dir in root.iterdir():
        if not user_dir.is_dir() or not _is_id(user_dir.name):
            continue
        for sess_dir in user_dir.iterdir():
            if not sess_dir.is_dir() or not _is_id(sess_dir.name):
                continue
            session = _read_json(sess_dir / "session.json")
            if session is None:
                continue
            timeline = _read_timeline(sess_dir / "timeline.jsonl")
            events = [str(e.get("event") or "") for e in timeline]
            summaries.append(_summary(user_dir.name, sess_dir.name, session, events))
    summaries.sort(key=lambda s: s.started_at, reverse=True)
    return summaries


def _segment_text(directory: Path, segment_id: str) -> str:
    if not _is_id(segment_id):
        return ""
    try:
        return (directory / segment_id / "transcript.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _event_detail(name: str, event: dict[str, Any]) -> str:
    if name == "settings_changed":
        return f"{event.get('persona')} / {event.get('llm_model')} / {event.get('voice')}"
    if name == "session_closed":
        return str(event.get("reason") or "")
    if name == "error_shown":
        return str(event.get("code") or "")
    if name == "segment_unusable":
        return f"ASR: {event.get('asr')}"
    if name == "interrupted":
        bits = [str(event.get("reason") or "")]
        if event.get("heard_ms") is not None:
            bits.append(f"{event.get('heard_ms')} ms gehört")
        return ", ".join(b for b in bits if b)
    if name == "stale_discarded":
        return str(event.get("stage") or "")
    if name == "presenter_alert":
        return str(event.get("kind") or "")
    return ""


def _turn_step(
    t: str, record: dict[str, Any], generation: str | None, reply: dict[str, Any] | None,
    has_audio: bool,
) -> Step:  # fmt: skip
    raw = record.get("llm_raw_outputs")
    llm_output = raw[-1] if isinstance(raw, list) and raw and isinstance(raw[-1], str) else ""
    settings = _as_dict(record.get("settings"))
    voice = str(settings.get("voice") or "")
    phrases: tuple[str, ...] = ()
    reply_status = ""
    if reply is not None:
        requested = reply.get("requested_speech_phrases")
        if isinstance(requested, list):
            phrases = tuple(p for p in requested if isinstance(p, str))
        voice = str(_as_dict(reply.get("config")).get("voice") or voice)
        reply_status = str(reply.get("status") or "")
    return Step(
        t=t,
        event="turn_started",
        merged_transcript=str(record.get("merged_transcript") or ""),
        llm_input=str(record.get("llm_input") or ""),
        llm_output=llm_output,
        outcome=_as_dict(record.get("outcome")),
        journal_change=_as_dict(record.get("journal_change")),
        reply=str(record.get("spoken_reply") or ""),
        voice=voice,
        tts_phrases=phrases,
        reply_status=reply_status,
        reply_audio_gen=generation if (has_audio and generation) else None,
    )


def load_replay(archive_root: Path, user_id: str, session_id: str) -> SessionReplay | None:
    """Reconstruct one session as ordered steps, or None if it cannot be resolved."""
    directory = session_dir(archive_root, user_id, session_id)
    if directory is None:
        return None
    session = _read_json(directory / "session.json") or {}
    timeline = _read_timeline(directory / "timeline.jsonl")

    utterances: dict[str, dict[str, Any]] = {}
    utterance_dir = directory / "utterances"
    if utterance_dir.is_dir():
        for file in utterance_dir.glob("*.json"):
            if _is_id(file.stem):
                record = _read_json(file)
                if record is not None:
                    utterances[file.stem] = record

    generation_of: dict[str, str] = {}
    for event in timeline:
        if event.get("event") == "reply_started":
            utt, gen = event.get("utterance"), event.get("generation")
            if _is_id(utt) and _is_id(gen):
                generation_of[utt] = gen

    def reply_for(generation: str | None) -> tuple[dict[str, Any] | None, bool]:
        if not _is_id(generation):
            return None, False
        for file in directory.glob(f"*/replies/{generation}/reply.json"):
            return _read_json(file), (file.parent / "speech.wav").is_file()
        return None, False

    steps: list[Step] = []
    for event in timeline:
        name = str(event.get("event") or "")
        when = _hms(event.get("t"))
        if name == "segment_saved":
            segment = event.get("segment")
            steps.append(
                Step(
                    t=when,
                    event=name,
                    verbatim=_segment_text(directory, segment) if _is_id(segment) else "",
                    asr_status=str(event.get("asr_status") or ""),
                    segment_id=segment if _is_id(segment) else None,
                )
            )
        elif name == "turn_started":
            utt = event.get("utterance")
            record = utterances.get(utt) if _is_id(utt) else None
            if record is None or not _is_id(utt):
                steps.append(Step(t=when, event=name, missing=True))
                continue
            generation = generation_of.get(utt)
            reply, has_audio = reply_for(generation)
            steps.append(_turn_step(when, record, generation, reply, has_audio))
        elif name in _FOLDED:
            utt = event.get("utterance")
            if isinstance(utt, str) and utt in utterances:
                continue  # already shown inside its turn block
            steps.append(Step(t=when, event=name, detail=_event_detail(name, event)))
        else:
            steps.append(Step(t=when, event=name, detail=_event_detail(name, event)))

    summary = _summary(user_id, session_id, session, [str(e.get("event") or "") for e in timeline])
    return SessionReplay(summary=summary, steps=tuple(steps))


# --- HTML rendering (presenter tab) -----------------------------------------------------------


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _audio(url: str) -> str:
    return f'<audio class="rv-audio" controls preload="none" src="{_esc(url)}"></audio>'


def _details(summary_text: str, body: str) -> str:
    return (
        f"<details class='rv-det'><summary>{_esc(summary_text)}</summary>"
        f"<pre>{_esc(body)}</pre></details>"
    )


def _sub(label: str, body: str) -> str:
    """A labelled sub-section: the bold German label plus an already-built HTML body."""
    return f"<div class='rv-sub'>{_esc(label)}</div>{body}"


def _audio_url(summary: SessionSummary, kind: str, item_id: str) -> str:
    return f"{AUDIO_BASE}/{summary.user_id}/{summary.session_id}/{kind}/{item_id}"


def _summary_html(s: SessionSummary) -> str:
    closed = f"{s.closed_at[:19].replace('T', ' ')} ({s.close_reason})" if s.closed_at else "läuft"
    rows = [
        ("Sitzung", f"{s.user_id} / {s.session_id}"),
        ("Gestartet", s.started_at[:19].replace("T", " ") or "?"),
        ("Beendet", closed),
        ("Einstellungen", f"Persona {s.persona} · Modell {s.model} · Stimme {s.voice}"),
        ("Antworten / Hinweise / Fehler", f"{s.turns} / {s.alerts} / {s.errors}"),
    ]
    items = "".join(
        f"<div class='rv-k'>{_esc(k)}</div><div class='rv-v'>{_esc(v)}</div>" for k, v in rows
    )
    return f"<div class='rv-summary'>{items}</div>"


def _metrics_html(step: Step) -> str:
    o = step.outcome
    bits: list[str] = []
    if o.get("source"):
        bits.append(_esc(o["source"]))
    if o.get("latency_ms") is not None:
        bits.append(f"{_esc(o['latency_ms'])} ms")
    tokens = o.get("tokens")
    if isinstance(tokens, list) and len(tokens) == 2:
        bits.append(f"{_esc(tokens[0])}/{_esc(tokens[1])} Tokens")
    if o.get("attempts"):
        bits.append(f"{_esc(o['attempts'])} Versuch(e)")
    if o.get("grounding") is not None:
        bits.append(f"Erdung {_esc(o['grounding'])}")
    badges = ""
    for flag in list(o.get("soft_flags") or []) + list(o.get("flags") or []):
        badges += f"<span class='rv-flag'>{_esc(flag)}</span>"
    for code in o.get("error_codes") or []:
        badges += f"<span class='rv-flag rv-err'>{_esc(code)}</span>"
    line = " · ".join(bits)
    return f"<div class='rv-metrics'>{line}{badges}</div>"


def _turn_html(step: Step, summary: SessionSummary) -> str:
    out = [f"<div class='rv-turn'><div class='rv-when'>{_esc(step.t)}</div><div class='rv-card'>"]
    out.append("<div class='rv-h'>💬 Antwort-Runde</div>")
    if step.merged_transcript:
        heard = f"<div class='rv-text'>{_esc(step.merged_transcript)}</div>"
        out.append(_sub("Gehört (zusammengefasst)", heard))
    if step.llm_input:
        out.append(_details("Eingabe an das Sprachmodell (Prompt)", step.llm_input))
    out.append(_metrics_html(step))
    if step.llm_output:
        out.append(_details("Rohausgabe des Sprachmodells", step.llm_output))
    jc = step.journal_change
    if jc:
        kind = _esc(jc.get("kind") or "")
        passage = _esc(jc.get("passage") or "")
        reason = jc.get("reason") or ""
        extra = f" — {_esc(reason)}" if reason else ""
        out.append(_sub("Tagebuch", f"<div class='rv-text'>{kind} {passage}{extra}</div>"))
        if jc.get("previous_text"):
            out.append(_details("Vorheriger Text des Eintrags", jc["previous_text"]))
    if step.reply:
        out.append(_sub("Gesprochene Antwort", f"<div class='rv-reply'>{_esc(step.reply)}</div>"))
    tts_bits = f"Stimme {_esc(step.voice)}" if step.voice else ""
    if step.reply_status:
        tts_bits += f" · {_esc(step.reply_status)}"
    head = f"An die Sprachausgabe (TTS){' · ' + tts_bits if tts_bits else ''}"
    if step.tts_phrases:
        phrases = "".join(f"<li>{_esc(p)}</li>" for p in step.tts_phrases)
        body = f"<ol class='rv-phrases'>{phrases}</ol>"
    else:
        body = "<div class='rv-text'><i>keine Phrasen</i></div>"
    out.append(f"<div class='rv-sub'>{head}</div>{body}")
    if step.reply_audio_gen:
        out.append(_audio(_audio_url(summary, "reply", step.reply_audio_gen)))
    out.append("</div></div>")
    return "".join(out)


def _segment_html(step: Step, summary: SessionSummary) -> str:
    text = _esc(step.verbatim) if step.verbatim else "<i>(kein Text)</i>"
    status = f"<span class='rv-badge'>{_esc(step.asr_status)}</span>" if step.asr_status else ""
    audio = _audio(_audio_url(summary, "segment", step.segment_id)) if step.segment_id else ""
    return (
        f"<div class='rv-seg'><div class='rv-when'>{_esc(step.t)}</div>"
        f"<div class='rv-card'><div class='rv-h'>👂 Gehört {status}</div>"
        f"<div class='rv-text'>{text}</div>{audio}</div></div>"
    )


def _event_html(step: Step) -> str:
    icon, label = _EVENT_LABELS.get(step.event, ("•", step.event))
    detail = f" — {_esc(step.detail)}" if step.detail else ""
    missing = " (nicht abgeschlossen)" if step.missing else ""
    return (
        f"<div class='rv-ev'><span class='rv-when'>{_esc(step.t)}</span>"
        f"<span class='rv-ev-label'>{_esc(icon)} {_esc(label)}{missing}{detail}</span></div>"
    )


def render_html(replay: SessionReplay) -> str:
    """The ordered session view as self-contained HTML (private content, all escaped)."""
    parts = [_summary_html(replay.summary)]
    if not replay.steps:
        parts.append("<p class='rv-empty'>Keine Ereignisse in dieser Sitzung.</p>")
    for step in replay.steps:
        if step.event == "segment_saved":
            parts.append(_segment_html(step, replay.summary))
        elif step.event == "turn_started" and not step.missing:
            parts.append(_turn_html(step, replay.summary))
        else:
            parts.append(_event_html(step))
    return "<div class='rv-replay'>" + "".join(parts) + "</div>"
