"""Presenter pre-flight checks: what can run by itself, what needs a person, what costs money.

- ``automatic``: free and silent; runs when the presenter opens the check (service probes, archive).
- ``interactive``: needs the browser or a human (microphone permission, hearing a test tone).
- ``paid``: a real TTS request; only ever runs after an explicit confirmation.

Every result is a short German sentence. Provider exception text is never shown or stored.
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from riva.demo.config import DemoConfig

Kind = Literal["automatic", "interactive", "paid"]
Status = Literal["ok", "warn", "fail", "manual"]
KIND_LABELS: dict[str, str] = {
    "automatic": "Läuft automatisch",
    "interactive": "Braucht dich (Browser)",
    "paid": "Kostet Geld (mit Bestätigung)",
}
STATUS_LABELS: dict[str, str] = {"ok": "✅ in Ordnung", "warn": "⚠️ Hinweis", "fail": "❌ Problem",
                                 "manual": "👆 noch zu prüfen"}  # fmt: skip


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    kind: Kind
    status: Status
    detail: str


class ProbeError(Exception):
    """A service probe failed; the message is never shown (it could contain private data)."""


class ServiceProbe(Protocol):
    def api_key_present(self) -> bool: ...
    def model_available(self, model: str) -> bool: ...
    def asr_reachable(self) -> bool: ...
    def synthesize(self, voice: str, tts_model: str, text: str) -> int:
        """PAID: one real TTS request; returns the number of audio bytes received."""
        ...


class OpenAIProbe:
    """Free probes (model lookup, opening an ASR session without audio) and one paid test."""

    def api_key_present(self) -> bool:
        import os

        return bool(os.environ.get("OPENAI_API_KEY", "").strip())

    def model_available(self, model: str) -> bool:
        from openai import NotFoundError, OpenAI

        try:
            with OpenAI(max_retries=0, timeout=10) as client:
                client.models.retrieve(model)
            return True
        except NotFoundError:
            return False
        except Exception as error:
            raise ProbeError from error

    def asr_reachable(self) -> bool:
        from riva.audio.transcription import connect_openai

        try:
            connect_openai().close()
            return True
        except Exception as error:
            raise ProbeError from error

    def synthesize(self, voice: str, tts_model: str, text: str) -> int:
        from riva.audio.replies import OpenAIReplyProvider, ReplyConfig

        try:
            config = ReplyConfig(tts_model=tts_model, voice=voice)
            return sum(len(chunk) for chunk in OpenAIReplyProvider().speech(text, config))
        except Exception as error:
            raise ProbeError from error


def _check(id_: str, label: str, kind: Kind, ok: bool, good: str, bad: str) -> Check:
    return Check(id_, label, kind, "ok" if ok else "fail", good if ok else bad)


def _probe(id_: str, label: str, fn, good: str, bad: str, down: str) -> Check:  # type: ignore[no-untyped-def]
    try:
        ok = bool(fn())
    except ProbeError:
        return Check(id_, label, "automatic", "fail", down)
    return _check(id_, label, "automatic", ok, good, bad)


ARCHIVE_BAD = "Der Ordner für die Aufnahmen ist nicht beschreibbar oder liegt in Git."
ARCHIVE_OK = "Aufnahmen und Texte können privat gespeichert werden."
NO_KEY_NOTE = "Ohne Schlüssel nicht prüfbar (mit --fake ist das in Ordnung)."
VOICE_NOTE = "Ob eine Stimme wirklich spricht, zeigt nur der Stimmtest."


def archive_writable(root: Path) -> Check:
    """Can the session archive be written (and is it outside Git)? Uses a temporary file."""
    from riva.audio.recordings import private_directory

    label = "Sitzungsordner beschreibbar"
    try:
        folder = private_directory(root)
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".preflight-"):
            pass
    except (OSError, ValueError):
        return Check("archive", label, "automatic", "fail", ARCHIVE_BAD)
    return Check("archive", label, "automatic", "ok", ARCHIVE_OK)


def run_automatic(config: DemoConfig, probe: ServiceProbe, archive_root: Path) -> list[Check]:
    checks = [archive_writable(archive_root)]
    has_key = probe.api_key_present()
    checks.append(_check("api_key", "OpenAI-Schlüssel gesetzt", "automatic", has_key,
                         "Der Schlüssel ist vorhanden (er wird nie angezeigt).",
                         "Es fehlt OPENAI_API_KEY."))  # fmt: skip
    if has_key:
        for model in config.models:
            checks.append(_probe(f"llm_{model.id}", f"Sprachmodell {model.id}",
                                 lambda m=model.id: probe.model_available(m),
                                 "Das Modell ist verfügbar.", "Das Modell ist nicht verfügbar.",
                                 "Der Dienst ist gerade nicht erreichbar."))  # fmt: skip
        checks.append(_probe("tts_model", f"Sprachausgabe {config.tts_model}",
                             lambda: probe.model_available(config.tts_model),
                             "Das Modell ist verfügbar.", "Das Modell ist nicht verfügbar.",
                             "Der Dienst ist gerade nicht erreichbar."))  # fmt: skip
        checks.append(_probe("asr", "Spracherkennung erreichbar", probe.asr_reachable,
                             "Eine Sitzung ließ sich öffnen.", "Keine Sitzung möglich.",
                             "Der Dienst ist gerade nicht erreichbar."))  # fmt: skip
    else:
        checks.append(Check("services", "Dienste erreichbar", "automatic", "warn", NO_KEY_NOTE))
    checks.append(Check("voices", "Stimmen eingestellt", "automatic",
                        "ok" if config.default_voice in config.voices else "fail",
                        f"{len(config.voices)} Stimmen, Standard: {config.default_voice}. "
                        + VOICE_NOTE))  # fmt: skip
    return checks


def interactive_checks() -> list[Check]:
    """Checks only a person at the browser can do; the presenter page runs them on button clicks."""
    return [
        Check("mic", "Mikrofon-Erlaubnis", "interactive", "manual",
              "Klicke auf „Mikrofon prüfen“ und erlaube den Zugriff im Browser."),
        Check("playback", "Ton abspielen", "interactive", "manual",
              "Klicke auf „Testton abspielen“ und bestätige, dass du ihn hörst."),
    ]  # fmt: skip


def paid_voice_test(
    probe: ServiceProbe, config: DemoConfig, voice: str, *, confirmed: bool
) -> Check:
    """One real TTS request for ``voice``. Refuses to run without an explicit confirmation."""
    label = f"Stimmtest {voice}"
    if not confirmed:
        raise PermissionError("The paid voice test needs explicit confirmation")
    if voice not in config.voices:
        return Check("voice_test", label, "paid", "fail", "Diese Stimme ist nicht erlaubt.")
    try:
        size = probe.synthesize(voice, config.tts_model, config.preview_sentence)
    except ProbeError:
        return Check(
            "voice_test", label, "paid", "fail", "Die Sprachausgabe hat nicht geantwortet."
        )
    if size <= 0:
        return Check("voice_test", label, "paid", "fail", "Es kam kein Ton zurück.")
    return Check(
        "voice_test", label, "paid", "ok", "Die Stimme hat gesprochen (kostet wenige Cent)."
    )


def as_rows(checks: list[Check]) -> list[list[str]]:
    """Table rows for the presenter page: group, check, result, detail."""
    return [[KIND_LABELS[c.kind], c.label, STATUS_LABELS[c.status], c.detail] for c in checks]
