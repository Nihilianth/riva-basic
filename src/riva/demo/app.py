"""Demo application: shared runtime (providers, ASR, replies, sessions) and the web server."""

import argparse
import ipaddress
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from riva.audio.recordings import RecordingStore, SessionIdentity
from riva.audio.replies import ReplyConfig, ReplyManager
from riva.audio.transcription import AsrConfig
from riva.demo.archive import SessionArchive
from riva.demo.config import DemoConfig, load_config
from riva.demo.fakes import FakeProbe
from riva.demo.preflight import OpenAIProbe, ServiceProbe
from riva.demo.providers import Providers, SpokenReplyProvider, fake_providers, real_providers
from riva.demo.session import DemoSession, SessionSettings
from riva.demo.turn import TurnProcessor
from riva.demo.voices import VoiceError, VoicePreviews
from riva.experiments.asr_stream import AsrSessions
from riva.experiments.audio_capture import prepare_environment
from riva.experiments.voice_reply import build_voice_api

if TYPE_CHECKING:
    import gradio as gr


HOST = "127.0.0.1"  # the demo listens on loopback only; there is no option to change it
MIN_PAUSE_MS, MAX_PAUSE_MS = 500, 3000
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
KEEP_CLOSED = 50  # closed sessions stay as records so a late poll cannot resurrect one


@dataclass
class DemoRuntime:
    """Everything shared by all sessions of one running demo.

    Session lifecycle - creating and closing are different, explicit things:
    - ``ensure(identity)`` CREATES a session the first time a page identity is seen. A page reload
      is a new identity, therefore a new, isolated session (own journal, archive folder, ASR turn).
    - ``reset(id)`` is the presenter's explicit reset: the session is closed immediately.
    - ``end_session(id)`` is the page's unload beacon: best-effort immediate close of the old page.
    - ``reap()`` expires abandoned sessions (no poll for ``idle_timeout`` seconds); ``release``
      is Gradio's own state cleanup. All of them end in the idempotent ``DemoSession.close``.
    A closed session stays in ``sessions`` as a record, rejects every late result (commit gate)
    and is never recreated for the same identity.
    """

    config: DemoConfig
    providers: Providers
    asr: AsrSessions
    replies: ReplyManager
    processor: TurnProcessor
    archive_root: Path
    defaults: SessionSettings
    idle_timeout: float = 180.0
    monotonic: Callable[[], float] = time.monotonic
    pause_ms: int = 900  # VAD pause threshold read by new participant pages (/riva/settings)
    probe: ServiceProbe | None = None
    voices: VoicePreviews | None = None
    sessions: dict[str, DemoSession] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    _last_reap: float = 0.0

    def ensure(self, identity: SessionIdentity) -> DemoSession:
        """The session for this page identity: created on first use, never recreated once closed."""
        self.reap_if_due()
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
                    monotonic=self.monotonic,
                )
                self.sessions[identity.session_id] = session
                self._prune_closed()
        session.touch()
        return session

    def set_defaults(self, settings: SessionSettings) -> None:
        """Choice for NEW sessions (validated against the allowlists)."""
        self.config.check_choice(settings.persona_id, settings.model, settings.voice)
        self.defaults = settings

    def set_pause_ms(self, value: int) -> int:
        if isinstance(value, bool) or not MIN_PAUSE_MS <= int(value) <= MAX_PAUSE_MS:
            raise ValueError(f"pause must be {MIN_PAUSE_MS}-{MAX_PAUSE_MS} ms")
        self.pause_ms = int(value)
        return self.pause_ms

    def stop_all(self) -> int:
        """ "Alles anhalten": end speech in every active session. Returns how many were stopped."""
        return sum(1 for session in self.active() if session.stop_speech())

    def get(self, session_id: str) -> DemoSession | None:
        with self.lock:
            return self.sessions.get(session_id)

    def active(self) -> list[DemoSession]:
        with self.lock:
            return [s for s in self.sessions.values() if not s.closed]

    def reset(self, session_id: str, reason: str = "presenter_reset") -> bool:
        """Close one session immediately. Returns False if it is unknown or already closed."""
        session = self.get(session_id) if SESSION_ID.fullmatch(session_id) else None
        if session is None or session.closed:
            return False
        session.close(reason)
        return True

    def reset_all(self, reason: str = "presenter_reset") -> int:
        return sum(self.reset(s.identity.session_id, reason) for s in self.active())

    def end_session(self, session_id: str) -> bool:
        """The page is going away (reload/close): close its session now."""
        return self.reset(session_id, "page_unloaded")

    def reap(self) -> list[str]:
        """Expire sessions whose page stopped polling; their records stay consistent."""
        expired = [s for s in self.active() if s.idle_seconds() > self.idle_timeout]
        for session in expired:
            session.close("expired")
        return [s.identity.session_id for s in expired]

    def reap_if_due(self) -> None:
        now = self.monotonic()
        with self.lock:
            if now - self._last_reap < 5.0:
                return
            self._last_reap = now
        self.reap()

    def release(self, identity: SessionIdentity) -> None:
        """Gradio discarded the page state (long after the page left): close, keep the record."""
        session = self.get(identity.session_id)
        if session is not None:
            session.close("released")
        else:
            self.asr.release(identity)

    def _prune_closed(self) -> None:
        closed = [sid for sid, s in self.sessions.items() if s.closed]
        for sid in closed[:-KEEP_CLOSED] if len(closed) > KEEP_CLOSED else []:
            del self.sessions[sid]


def build_runtime(
    config: DemoConfig,
    providers: Providers,
    archive_root: Path,
    defaults: SessionSettings | None = None,
    *,
    idle_timeout: float = 180.0,
    monotonic: Callable[[], float] = time.monotonic,
    probe: ServiceProbe | None = None,
) -> DemoRuntime:
    defaults = defaults or SessionSettings(
        config.default_persona, config.default_model, config.default_voice
    )
    config.check_choice(defaults.persona_id, defaults.model, defaults.voice)
    asr = AsrSessions(RecordingStore(archive_root), AsrConfig(), providers.wire_factory)
    replies = ReplyManager(ReplyConfig(), lambda: SpokenReplyProvider(providers.speech))
    return DemoRuntime(
        config, providers, asr, replies, TurnProcessor(providers.llm, config), archive_root,
        defaults, idle_timeout, monotonic, probe=probe,
        voices=VoicePreviews(
            archive_root.parent / "demo-1-cache" / "voice-previews", providers.speech, config
        ),
    )  # fmt: skip


class LocalOnlyPresenter:
    """ASGI guard: /presenter (session content, resets, paid tests) answers loopback clients only.

    Defense in depth. The server itself already binds to 127.0.0.1; if someone ever exposes it
    (another bind address, a tunnel, a reverse proxy) the presenter page must still not be reachable
    without real access control, which does not exist yet. Behind a proxy the client address is the
    proxy's, so exposing the demo that way is not supported.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket") and scope["path"].startswith("/presenter"):
            client = scope.get("client")
            host = client[0] if client else ""
            if not is_loopback(host):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await PlainTextResponse("Nur lokal erreichbar.", status_code=403)(
                        scope, receive, send
                    )
                return
        await self.app(scope, receive, send)


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_demo_api(runtime: DemoRuntime) -> FastAPI:
    from riva.demo import presenter
    from riva.demo.ui import CSS, HEAD, INDICATOR_JS, build_participant_app  # noqa: F401

    app: gr.Blocks = build_participant_app(runtime)
    presenter_app = presenter.build_presenter_app(runtime)

    def lifecycle_routes(api: FastAPI) -> None:
        @api.get("/riva/settings")
        async def settings() -> dict[str, int]:
            """Pause threshold set by the presenter; read by the VAD when Start is pressed."""
            return {"redemption_ms": runtime.pause_ms}

        @api.get("/riva/voice_preview/{voice}")
        async def voice_preview(voice: str) -> FileResponse:
            """The cached preview sentence in one voice (generated on first use)."""
            if runtime.voices is None or voice not in runtime.config.voices:
                raise HTTPException(status_code=404)
            try:
                path = await anyio.to_thread.run_sync(runtime.voices.get, voice)
            except VoiceError:
                raise HTTPException(status_code=502) from None
            return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})

        @api.post("/riva/session_end")
        async def session_end(request: Request) -> dict[str, bool]:
            """Unload beacon from the page: close that page's session now (best effort)."""
            try:
                data = await request.json()
                return {"closed": runtime.end_session(str(data["session_id"]))}
            except (ValueError, KeyError, TypeError):
                return {"closed": False}

    api = build_voice_api(
        app, runtime.replies, css=CSS, head=HEAD, footer_links=[], extra_routes=lifecycle_routes,
        extra_apps=[("/presenter", presenter_app, {"css": presenter.CSS, "head": presenter.HEAD,
                                                  "footer_links": [], "show_error": False})],
    )  # fmt: skip
    api.add_middleware(LocalOnlyPresenter)
    return api


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
    runtime = build_runtime(
        config,
        providers,
        root / "research-data" / "demo-1",
        probe=FakeProbe() if args.fake else OpenAIProbe(),
    )
    mode = "FAKE providers, no paid calls" if args.fake else "OpenAI ASR, LLM and TTS (paid)"
    print(f"Demo 1: http://localhost:{args.port}  [{mode}]")
    uvicorn.run(build_demo_api(runtime), host=HOST, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
