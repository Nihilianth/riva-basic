"""Demo application: shared runtime (providers, ASR, replies, sessions) and the web server."""

import argparse
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI

from riva.audio.recordings import RecordingStore, SessionIdentity
from riva.audio.replies import ReplyConfig, ReplyManager
from riva.audio.transcription import AsrConfig
from riva.demo.archive import SessionArchive
from riva.demo.config import DemoConfig, load_config
from riva.demo.providers import Providers, SpokenReplyProvider, fake_providers, real_providers
from riva.demo.session import DemoSession, SessionSettings
from riva.demo.turn import TurnProcessor
from riva.experiments.asr_stream import AsrSessions
from riva.experiments.audio_capture import prepare_environment
from riva.experiments.voice_reply import build_voice_api

if TYPE_CHECKING:
    import gradio as gr


@dataclass
class DemoRuntime:
    """Everything shared by all sessions of one running demo."""

    config: DemoConfig
    providers: Providers
    asr: AsrSessions
    replies: ReplyManager
    processor: TurnProcessor
    archive_root: Path
    defaults: SessionSettings
    sessions: dict[str, DemoSession] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def ensure(self, identity: SessionIdentity) -> DemoSession:
        """The session for this page load, created on first use (a reload has a new identity)."""
        with self.lock:
            session = self.sessions.get(identity.session_id)
            if session is None:
                session = DemoSession(
                    identity=identity,
                    config=self.config,
                    settings=self.defaults,
                    processor=self.processor,
                    asr=self.asr,
                    replies=self.replies,
                    archive=SessionArchive(self.archive_root, identity),
                )
                self.sessions[identity.session_id] = session
            return session

    def release(self, identity: SessionIdentity) -> None:
        """Called when Gradio discards the page state; also the presenter's reset path."""
        with self.lock:
            session = self.sessions.pop(identity.session_id, None)
        if session is not None:
            session.close("released")
        else:
            self.asr.release(identity)


def build_runtime(
    config: DemoConfig,
    providers: Providers,
    archive_root: Path,
    defaults: SessionSettings | None = None,
) -> DemoRuntime:
    defaults = defaults or SessionSettings(
        config.default_persona, config.default_model, config.default_voice
    )
    config.check_choice(defaults.persona_id, defaults.model, defaults.voice)
    asr = AsrSessions(RecordingStore(archive_root), AsrConfig(), providers.wire_factory)
    replies = ReplyManager(ReplyConfig(), lambda: SpokenReplyProvider(providers.speech))
    return DemoRuntime(
        config, providers, asr, replies, TurnProcessor(providers.llm, config), archive_root,
        defaults,
    )  # fmt: skip


def build_demo_api(runtime: DemoRuntime) -> FastAPI:
    from riva.demo.ui import CSS, HEAD, INDICATOR_JS, build_participant_app  # noqa: F401

    app: gr.Blocks = build_participant_app(runtime)
    return build_voice_api(app, runtime.replies, css=CSS, head=HEAD, footer_links=[])


def main() -> None:
    parser = argparse.ArgumentParser(description="German journaling voice demo (Demo 1).")
    parser.add_argument(
        "--private-dir", type=Path, default=Path(os.environ.get("RIVA_PRIVATE_DIR", "../private"))
    )
    parser.add_argument("--port", type=int, default=7863)
    parser.add_argument("--config-dir", type=Path, default=None, help="folder with TOML overrides")
    parser.add_argument("--fake", action="store_true", help="offline: fake ASR, LLM and TTS")
    args = parser.parse_args()
    if not args.fake and not os.environ.get("OPENAI_API_KEY", "").strip():
        parser.error(
            "OPENAI_API_KEY missing. Run: uv run --env-file .env riva-demo (or use --fake)"
        )
    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")
    import uvicorn

    config = load_config(args.config_dir)
    root = prepare_environment(args.private_dir)
    providers = fake_providers() if args.fake else real_providers()
    runtime = build_runtime(config, providers, root / "research-data" / "demo-1")
    mode = "FAKE providers, no paid calls" if args.fake else "OpenAI ASR, LLM and TTS (paid)"
    print(f"Demo 1: http://localhost:{args.port}  [{mode}]")
    uvicorn.run(build_demo_api(runtime), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
