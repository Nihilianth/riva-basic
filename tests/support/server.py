"""Run the real Gradio voice app with fake providers on an ephemeral port for browser tests."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn

from riva.audio.recordings import RecordingStore
from riva.audio.replies import ReplyConfig, ReplyManager
from riva.audio.transcription import AsrConfig
from riva.experiments.asr_stream import AsrSessions, build_vad_app
from riva.experiments.vad_self_test import SyntheticReplyProvider, SyntheticWire
from riva.experiments.voice_reply import build_voice_api


@dataclass
class RunningApp:
    url: str
    sessions: AsrSessions
    replies: ReplyManager


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def running_voice_app(
    root: Path,
    *,
    wire: Any = SyntheticWire,
    provider: Any = SyntheticReplyProvider,
) -> Iterator[RunningApp]:
    """Real VAD page (continuous listening on), mocked ASR/LLM/TTS, archive under ``root``."""
    sessions = AsrSessions(RecordingStore(root / "archive"), AsrConfig(), wire)
    replies = ReplyManager(ReplyConfig(), provider)
    api = build_voice_api(build_vad_app(sessions, replies), replies)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="riva-test-server")
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("test server did not start")
    try:
        yield RunningApp(f"http://127.0.0.1:{port}", sessions, replies)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@dataclass
class RunningDemo:
    url: str
    runtime: Any


@contextmanager
def running_demo_app(root: Path, providers: Any) -> Iterator[RunningDemo]:
    """The real demo page (participant UI) on an ephemeral port with the given fake providers."""
    from riva.demo.app import build_demo_api, build_runtime
    from riva.demo.config import load_config

    runtime = build_runtime(load_config(), providers, root / "archive")
    api = build_demo_api(runtime)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="riva-demo-test-server")
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("demo test server did not start")
    try:
        yield RunningDemo(f"http://127.0.0.1:{port}", runtime)
    finally:
        for session in list(runtime.sessions.values()):
            session.close("test_end")
        server.should_exit = True
        thread.join(timeout=10)
