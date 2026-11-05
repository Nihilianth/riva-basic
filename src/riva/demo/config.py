"""Demo configuration (personas, models, voices) loaded from TOML files, not hardcoded.

The packaged defaults live in ``riva/demo/defaults``. A directory given explicitly or through
``RIVA_DEMO_CONFIG`` may contain ``personas.toml`` and/or ``settings.toml``; each file found there
replaces the packaged one as a whole.
"""

import os
import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from riva.demo.text import looks_german

ID_PATTERN = re.compile(r"[a-z0-9_-]{1,32}")
OPENAI_VOICES = frozenset(
    {"alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"}
)
MAX_PROMPT_CHARS = 8000
MAX_PREVIEW_CHARS = 120
ENV_DIR = "RIVA_DEMO_CONFIG"


class ConfigError(ValueError):
    """The demo configuration is invalid; the message names the setting, never a secret."""


@dataclass(frozen=True)
class Persona:
    id: str
    label: str
    prompt: str
    tts_style: str


@dataclass(frozen=True)
class ModelOption:
    id: str
    label: str


@dataclass(frozen=True)
class DemoConfig:
    core_prompt: str
    personas: tuple[Persona, ...]
    default_persona: str
    models: tuple[ModelOption, ...]
    default_model: str
    tts_model: str
    voices: tuple[str, ...]
    default_voice: str
    preview_sentence: str
    history_exchanges: int
    journal_tail: int
    llm_timeout_seconds: float

    def check_choice(self, persona_id: str, model: str, voice: str) -> None:
        """Reject anything outside the configured allowlists (presenter choices are untrusted)."""
        self.persona(persona_id)
        if model not in {m.id for m in self.models}:
            raise ConfigError(f"model is not allowed: {model}")
        if voice not in self.voices:
            raise ConfigError(f"voice is not allowed: {voice}")

    def persona(self, persona_id: str) -> Persona:
        for persona in self.personas:
            if persona.id == persona_id:
                return persona
        raise ConfigError(f"unknown persona: {persona_id}")


def _text(value: Any, name: str, limit: int = MAX_PROMPT_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be non-empty text")
    if len(value) > limit:
        raise ConfigError(f"{name} is too long")
    return value.strip()


def _ident(value: Any, name: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ConfigError(f"{name} must match {ID_PATTERN.pattern}")
    return value


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return int(value)


def _read(name: str, directory: Path | None) -> dict[str, Any]:
    override = directory or (Path(os.environ[ENV_DIR]) if os.environ.get(ENV_DIR) else None)
    if override is not None and (override / name).is_file():
        raw = (override / name).read_text(encoding="utf-8")
    else:
        raw = (resources.files("riva.demo") / "defaults" / name).read_text(encoding="utf-8")
    try:
        return tomllib.loads(raw)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{name} is not valid TOML") from error


def load_config(directory: Path | None = None) -> DemoConfig:
    personas_doc = _read("personas.toml", directory)
    settings = _read("settings.toml", directory)
    try:
        core = _text(personas_doc["core"]["prompt"], "core prompt")
        raw_personas = personas_doc["personas"]
        llm, tts, limits = settings["llm"], settings["tts"], settings["limits"]
    except (KeyError, TypeError) as error:
        raise ConfigError("a required section is missing") from error

    personas = []
    for item in raw_personas:
        personas.append(
            Persona(
                _ident(item.get("id"), "persona id"),
                _text(item.get("label"), "persona label", 60),
                _text(item.get("prompt"), "persona prompt"),
                _text(item.get("tts_style"), "persona tts_style", 300),
            )
        )
    if len({p.id for p in personas}) != len(personas) or not personas:
        raise ConfigError("persona ids must be unique and at least one persona is required")

    models = tuple(
        ModelOption(_text(m.get("id"), "model id", 80), _text(m.get("label"), "model label", 80))
        for m in llm.get("models", [])
    )
    if not models:
        raise ConfigError("at least one LLM model is required")
    default_model = _text(llm.get("default"), "default model", 80)
    if default_model not in {m.id for m in models}:
        raise ConfigError("default model is not in the model list")

    voices = tuple(tts.get("voices", []))
    if not voices or any(v not in OPENAI_VOICES for v in voices):
        raise ConfigError(f"voices must be a subset of {sorted(OPENAI_VOICES)}")
    default_voice = _text(tts.get("default_voice"), "default voice", 40)
    if default_voice not in voices:
        raise ConfigError("default voice is not in the voice list")
    preview = _text(tts.get("preview_sentence"), "preview sentence", MAX_PREVIEW_CHARS)
    if not looks_german(preview):
        raise ConfigError("preview sentence must be German")

    config = DemoConfig(
        core_prompt=core,
        personas=tuple(personas),
        default_persona=personas[0].id,
        models=models,
        default_model=default_model,
        tts_model=_text(tts.get("model"), "tts model", 80),
        voices=voices,
        default_voice=default_voice,
        preview_sentence=preview,
        history_exchanges=_positive(limits.get("history_exchanges"), "history_exchanges"),
        journal_tail=_positive(limits.get("journal_tail"), "journal_tail"),
        llm_timeout_seconds=float(
            _positive(limits.get("llm_timeout_seconds"), "llm_timeout_seconds")
        ),
    )
    return config
