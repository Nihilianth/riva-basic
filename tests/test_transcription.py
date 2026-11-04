import base64
import json
import queue
import threading
from pathlib import Path

import numpy as np
import pytest

from riva.audio.recordings import RecordingStore, new_development_session
from riva.audio.transcription import AsrConfig, LiveTurn, PcmEncoder, Transcript, mono_float
from riva.audio.transcripts import save_transcript
from riva.experiments.asr_stream import AsrSessions, build_asr_app, build_vad_app


class FakeWire:
    def __init__(self, *, complete: bool = True, error: bool = False) -> None:
        self.events: queue.Queue[dict] = queue.Queue()
        self.sent: list[dict] = []
        self.complete = complete
        self.error = error
        self.closed = False
        self.partial_received = threading.Event()
        self.delta_sent = False

    def send(self, message: str) -> None:
        event = json.loads(message)
        self.sent.append(event)
        if event["type"] == "session.update":
            self.events.put({"type": "session.updated"})
        elif event["type"] == "input_audio_buffer.append" and not self.delta_sent:
            self.delta_sent = True
            if self.error:
                self.events.put(
                    {"type": "error", "error": {"message": "sensitive synthetic payload"}}
                )
            else:
                self.events.put(
                    {
                        "type": "conversation.item.input_audio_transcription.delta",
                        "item_id": "synthetic-item",
                        "delta": "Heute war",
                    }
                )
        elif event["type"] == "input_audio_buffer.commit":
            self.events.put({"type": "input_audio_buffer.committed", "item_id": "synthetic-item"})
            if self.complete:
                self.events.put(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "synthetic-item",
                        "transcript": "Heute war ich im Park.",
                    }
                )

    def recv(self, timeout: float | None = None) -> str:
        try:
            event = self.events.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        if event["type"].endswith(".delta"):
            self.partial_received.set()
        return json.dumps(event)

    def close(self) -> None:
        self.closed = True


def audio() -> tuple[int, np.ndarray]:
    return 48000, np.zeros(24000, dtype=np.int16)


def test_streaming_turn_uses_one_connection_and_commits_after_audio() -> None:
    wire = FakeWire()
    calls = []

    def factory() -> FakeWire:
        calls.append(True)
        return wire

    turn = LiveTurn(AsrConfig(), factory)
    try:
        turn.append(audio())
        assert wire.partial_received.wait(2)
        turn.append(audio())
        result = turn.finish()
        assert result.status == "completed"
        assert turn.audio_sent.is_set()
        assert turn.commit_sent.is_set()
        assert result.text == "Heute war ich im Park."
        assert result.partial == "Heute war"
        assert calls == [True]
        types = [event["type"] for event in wire.sent]
        assert types[0] == "session.update"
        assert types[-1] == "input_audio_buffer.commit"
        assert types.count("input_audio_buffer.commit") == 1
        pcm = b"".join(base64.b64decode(e["audio"]) for e in wire.sent if "audio" in e)
        assert len(pcm) == 24000 * 2
        assert wire.closed
        assert turn.finish() == result
    finally:
        turn.close()


def test_configuration_is_transcription_only_with_manual_commit() -> None:
    session = AsrConfig().update_event()["session"]
    assert session["type"] == "transcription"
    settings = session["audio"]["input"]
    assert settings["turn_detection"] is None
    assert settings["transcription"]["languages"] == ["de"]
    assert settings["format"] == {"type": "audio/pcm", "rate": 24000}
    assert (
        "languages"
        not in AsrConfig(model="gpt-transcribe").update_event()["session"]["audio"]["input"][
            "transcription"
        ]
    )


def test_stateful_resampling_preserves_chunk_boundaries() -> None:
    samples = (np.sin(np.arange(44100) * 0.02) * 0.2).astype(np.float32)
    whole = PcmEncoder(44100).encode(samples, last=True)
    encoder = PcmEncoder(44100)
    split = b"".join(encoder.encode(chunk) for chunk in np.array_split(samples, 13))
    split += encoder.encode(np.empty(0, dtype=np.float32), last=True)
    assert len(split) == len(whole) == 24000 * 2
    np.testing.assert_allclose(np.frombuffer(split, "<i2"), np.frombuffer(whole, "<i2"), atol=1)


def test_stereo_downmix_does_not_peak_normalize() -> None:
    rate, mono = mono_float((24000, np.array([[32767, -32767], [16384, 16384]], dtype=np.int16)))
    assert rate == 24000
    np.testing.assert_allclose(mono, [0, 0.5])
    assert mono.dtype == np.float32


def test_provider_error_is_sanitized_and_input_survives() -> None:
    wire = FakeWire(error=True)
    turn = LiveTurn(AsrConfig(), lambda: wire)
    try:
        turn.append(audio())
        assert turn.done.wait(2)
        turn.append(audio())
        result = turn.finish()
        assert result.status == "failed"
        assert result.error_code == "provider_error"
        assert result.text is None
        assert "sensitive" not in repr(result)
        assert len(turn.audio()[1]) == 48000
    finally:
        turn.close()


def test_final_timeout_never_promotes_partial_text() -> None:
    turn = LiveTurn(AsrConfig(final_timeout=0.05), lambda: FakeWire(complete=False))
    try:
        turn.append(audio())
        result = turn.finish()
        assert result.error_code == "final_timeout"
        assert result.text is None
    finally:
        turn.close()


def test_disconnected_client_expires_idle_connection() -> None:
    turn = LiveTurn(AsrConfig(idle_timeout=0.05), FakeWire)
    try:
        assert turn.done.wait(2)
        assert turn.snapshot().error_code == "idle_timeout"
    finally:
        turn.close()


def test_connection_failure_is_sanitized() -> None:
    def factory() -> FakeWire:
        raise OSError("sensitive synthetic auth failure")

    turn = LiveTurn(AsrConfig(), factory)
    try:
        assert turn.done.wait(2)
        turn.append(audio())
        assert turn.finish().error_code == "connection_failed"
    finally:
        turn.close()


def test_different_item_cannot_finalize_the_turn() -> None:
    turn = LiveTurn(AsrConfig(), FakeWire)
    try:
        turn._event(
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": "one",
                "delta": "Hallo",
            },
            False,
        )
        turn._event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "other",
                "transcript": "Wrong",
            },
            True,
        )
        assert turn.snapshot().error_code == "unexpected_item"
        assert turn.snapshot().text is None
    finally:
        turn.close()


def test_duration_limit_and_late_chunks_are_rejected() -> None:
    turn = LiveTurn(AsrConfig(max_seconds=0.1), FakeWire)
    try:
        with pytest.raises(ValueError, match="duration"):
            turn.append(audio())
        assert turn.snapshot().error_code == "duration_limit"
        turn.finish()
        with pytest.raises(ValueError, match="committed"):
            turn.append(audio())
    finally:
        turn.close()


@pytest.mark.parametrize("status,text", [("completed", "Heute war ich im Park."), ("failed", None)])
def test_transcript_artifacts_are_private_and_honest(
    tmp_path: Path, status: str, text: str | None
) -> None:
    saved = RecordingStore(tmp_path).save(audio(), new_development_session())
    save_transcript(
        saved,
        Transcript(status, "vorläufig", text, error_code="test" if text is None else None),
        AsrConfig(),
    )
    root = saved.audio_path.parent
    record = json.loads((root / "transcript.json").read_text())
    assert record["text"] == text
    assert record["segments"] == []
    assert record["timestamp_source"] == "unavailable"
    assert (root / "transcript.txt").exists() == (status == "completed")
    assert (root / "transcript.json").stat().st_mode & 0o777 == 0o600
    assert json.loads(saved.metadata_path.read_text())["transcription_status"] == status


def test_session_finish_saves_once_and_keeps_users_isolated(tmp_path: Path) -> None:
    sessions = AsrSessions(RecordingStore(tmp_path), AsrConfig(), FakeWire)
    first, second = new_development_session(), new_development_session()
    try:
        sessions.begin(first)
        sessions.begin(second)
        sessions.append(audio(), first)
        with pytest.raises(ValueError, match="Stop"):
            sessions.finish(first)
        sessions.current(first).stopped = True
        saved = sessions.finish(first)
        assert sessions.finish(first).audio_path == saved.audio_path
        assert sessions.snapshot(second)[1] == 0
        assert len(list(tmp_path.glob("*/*/*/recording.wav"))) == 1
    finally:
        sessions.release(first)
        sessions.release(second)


def test_asr_ui_construction_makes_no_provider_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))
    from fastapi.testclient import TestClient

    def forbidden() -> FakeWire:
        pytest.fail("Constructing/visiting the UI must not call OpenAI")

    app = build_asr_app(AsrSessions(RecordingStore(tmp_path / "archive"), AsrConfig(), forbidden))
    with TestClient(app.app) as client:
        config = client.get("/config")
    assert config.status_code == 200
    assert app.allowed_paths == []
    assert "OpenAI" in str(config.json())


def test_asr_callbacks_save_final_text_and_cache_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))
    sessions = AsrSessions(RecordingStore(tmp_path / "archive"), AsrConfig(), FakeWire)
    import gradio as gr

    from riva.experiments.gradio_delivery import DELIVERY_FIELD

    identity = new_development_session()
    app = build_asr_app(sessions)
    handlers = {fn.fn.__name__: fn.fn for fn in app.fns.values() if fn.fn}
    try:
        handlers["begin"](identity)
        handlers["append"](audio(), identity, gr.EventData(None, {DELIVERY_FIELD: "a" * 32}))
        handlers["stopped"](identity)
        playback, details = handlers["finish"](identity)
        assert Path(playback).is_relative_to(tmp_path / "cache")
        assert details["transcription_status"] == "completed"
        assert handlers["poll"](identity)[1] == "Heute war ich im Park."
        saved = sessions.current(identity).saved
        assert saved is not None
        assert saved.audio_path.read_bytes() == Path(playback).read_bytes()
        assert (
            saved.audio_path.parent / "transcript.txt"
        ).read_text() == "Heute war ich im Park.\n"
    finally:
        sessions.release(identity)


def test_invalid_chunk_fails_the_turn_instead_of_silently_skipping() -> None:
    turn = LiveTurn(AsrConfig(), FakeWire)
    try:
        with pytest.raises(ValueError):
            turn.append((24000, np.array([np.nan], dtype=np.float32)))
        assert turn.snapshot().error_code == "invalid_audio"
    finally:
        turn.close()


def test_backpressure_never_silently_drops_asr_audio() -> None:
    ready = threading.Event()

    def factory() -> FakeWire:
        ready.wait(2)
        return FakeWire()

    turn = LiveTurn(AsrConfig(), factory)
    try:
        for _ in range(65):
            turn.append((24000, np.zeros(240, dtype=np.int16)))
        assert turn.snapshot().error_code == "backpressure"
        assert len(turn.audio()[1]) == 65 * 240
        ready.set()
        assert turn.done.wait(2)
        assert turn.snapshot().error_code == "backpressure"
    finally:
        ready.set()
        turn.close()


def test_safe_provider_error_codes_do_not_expose_messages() -> None:
    from riva.audio.transcription import provider_error_code

    assert provider_error_code({"error": {"code": "model_not_found"}}) == "model_unavailable"
    assert provider_error_code({"error": {"code": "private-text"}}) == "provider_error"


def test_late_browser_audio_fails_instead_of_merging_recordings(tmp_path: Path) -> None:
    sessions = AsrSessions(RecordingStore(tmp_path), AsrConfig(), FakeWire)
    identity = new_development_session()
    try:
        sessions.begin(identity)
        sessions.append(audio(), identity)
        sessions.current(identity).stopped = True
        with pytest.raises(ValueError, match="stop boundary"):
            sessions.append(audio(), identity)
        saved = sessions.finish(identity)
        record = json.loads((saved.audio_path.parent / "transcript.json").read_text())
        assert record["error_code"] == "audio_after_stop"
        assert record["text"] is None
    finally:
        sessions.release(identity)


def test_missing_key_fails_before_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    from riva.audio.transcription import connect_openai

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="missing_api_key"):
        connect_openai()


def test_replayed_delivery_is_ignored_but_identical_new_audio_is_kept(tmp_path: Path) -> None:
    sessions = AsrSessions(RecordingStore(tmp_path), AsrConfig(), FakeWire)
    identity = new_development_session()
    try:
        sessions.begin(identity)
        chunk = audio()
        sessions.append(chunk, identity, delivery_id="a" * 32)
        sessions.append(chunk, identity, delivery_id="a" * 32)
        assert sessions.snapshot(identity)[1] == 0.5
        # Identical audio in a new delivery is legitimate, not a semantic duplicate.
        sessions.append(chunk, identity, delivery_id="b" * 32)
        assert sessions.snapshot(identity)[1] == 1
        sessions.current(identity).stopped = True
        sessions.append(chunk, identity, delivery_id="b" * 32)
        assert sessions.current(identity).replays_skipped == 2
        saved = sessions.finish(identity)
        assert saved.metadata.duration_seconds == 1
        assert sessions.snapshot(identity)[0].status == "completed"
    finally:
        sessions.release(identity)


def test_real_gradio_routes_preserve_delivery_identity_on_stream_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))
    import asyncio

    import gradio as gr
    from fastapi.testclient import TestClient
    from gradio.helpers import special_args
    from gradio.routes import App

    from riva.experiments.asr_stream import asr_app_kwargs
    from riva.experiments.gradio_delivery import DELIVERY_FIELD

    sessions = AsrSessions(RecordingStore(tmp_path / "archive"), AsrConfig(), FakeWire)
    app = build_asr_app(sessions)
    index = next(i for i, fn in app.fns.items() if fn.fn and fn.fn.__name__ == "append")
    handler = app.fns[index].fn
    server = App.create_app(app, app_kwargs=asr_app_kwargs(app), mcp_server=False)
    event = None

    async def capture_push(body, request, username):
        nonlocal event
        event = SimpleNamespace(data=body, signal=asyncio.Event(), run_time=0)
        app._queue.event_ids_to_events["synthetic-stream"] = event
        return True, "synthetic-stream", "success"

    monkeypatch.setattr(app._queue, "push", capture_push)
    identity = new_development_session()
    sessions.begin(identity)

    def dispatch(body):
        args, *_ = special_args(
            handler, inputs=[audio(), identity], event_data=gr.EventData(None, body.event_data)
        )
        handler(*args)

    try:
        with TestClient(server) as client:
            body = {"fn_index": index, "data": [], "session_hash": "synthetic-session"}
            assert client.post("/gradio_api/queue/join", json=body).status_code == 200
            assert event is not None
            first_body = event.data
            dispatch(first_body)
            assert client.post("/gradio_api/stream/synthetic-stream", json=body).status_code == 200
            second_body = event.data
            assert first_body.event_data[DELIVERY_FIELD] != second_body.event_data[DELIVERY_FIELD]
            dispatch(second_body)
            assert client.post("/gradio_api/stream/synthetic-stream/close").status_code == 200
            assert event.data is second_body
            # The queue close wakeup can now re-dispatch this exact body safely.
            dispatch(event.data)
        assert sessions.snapshot(identity)[1] == 1
        assert sessions.current(identity).replays_skipped == 1
        sessions.current(identity).stopped = True
        assert sessions.finish(identity).metadata.duration_seconds == 1
    finally:
        sessions.release(identity)


def test_voice_ui_chains_only_saved_final_transcripts_and_reports_playback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))
    from riva.audio.replies import ReplyConfig, ReplyManager, TextPart

    class Provider:
        calls = 0

        def text(self, transcript, config):
            self.calls += 1
            assert transcript == "Heute war ich im Park."
            yield TextPart("delta", "Wie war es dort?")
            yield TextPart("completed", "Wie war es dort?")

        def speech(self, phrase, config):
            yield b"\0\0" * 12000

    provider = Provider()
    sessions = AsrSessions(RecordingStore(tmp_path / "archive"), AsrConfig(), FakeWire)
    replies = ReplyManager(ReplyConfig(), lambda: provider)
    identity = new_development_session()
    app = build_asr_app(sessions, replies)
    handlers = {fn.fn.__name__: fn.fn for fn in app.fns.values() if fn.fn}
    try:
        assert provider.calls == 0
        sessions.begin(identity)
        sessions.append(audio(), identity)
        sessions.current(identity).stopped = True
        sessions.finish(identity)
        assert handlers["enable"](identity).interactive
        outputs = list(handlers["reply"](identity, 100))
        assert outputs[-1][0] == "Wie war es dort?"
        assert outputs[-1][2]["status"] == "completed"
        assert "abgeschlossen" in outputs[-1][3]
        wav_chunks = [v[1] for v in outputs if isinstance(v[1], bytes)]
        assert wav_chunks
        assert wav_chunks
        # Verify Gradio accepts our independent WAV frames and can encode/flush
        # them through its real streaming output path. This is not a browser test.
        import asyncio

        speech = next(c for c in app.blocks.values() if getattr(c, "label", None) == "KI-Stimme")

        async def encode():
            chunks = []
            for i, wav in enumerate(wav_chunks):
                chunk, _ = await speech.stream_output(wav, "synthetic-voice-test", i == 0)
                if chunk:
                    chunks.append(chunk)
            flushed = await speech.flush_stream_output("synthetic-voice-test")
            if flushed:
                chunks.append(flushed)
            return chunks

        assert asyncio.run(encode())
        metrics = outputs[-1][2]
        assert metrics["first_browser_play_event_ms"] is None
        play_result = handlers["played"](identity, {}, 300)
        assert play_result[0]["first_browser_play_event_ms"] == 200
        assert "Wiedergabe" in play_result[1]
        play_result = handlers["played"](identity, {"generation_id": "stale"}, 500)
        assert play_result[0]["first_browser_play_event_ms"] == 200
        list(handlers["reply"](identity, 500))
        assert provider.calls == 1
    finally:
        replies.release(identity)
        sessions.release(identity)


def test_failed_asr_never_starts_reply_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))
    import gradio as gr

    from riva.audio.replies import ReplyConfig, ReplyManager

    def forbidden():
        pytest.fail("Failed ASR must not trigger an LLM/TTS request")

    sessions = AsrSessions(
        RecordingStore(tmp_path / "archive"), AsrConfig(), lambda: FakeWire(error=True)
    )
    replies = ReplyManager(ReplyConfig(), forbidden)
    identity = new_development_session()
    app = build_asr_app(sessions, replies)
    handlers = {fn.fn.__name__: fn.fn for fn in app.fns.values() if fn.fn}
    try:
        sessions.begin(identity)
        sessions.append(audio(), identity)
        assert sessions.current(identity).live.done.wait(2)
        sessions.current(identity).stopped = True
        sessions.finish(identity)
        assert not handlers["enable"](identity).interactive
        with pytest.raises(gr.Error):
            list(handlers["reply"](identity, 0))
    finally:
        replies.release(identity)
        sessions.release(identity)


def test_manual_mode_remains_manual_and_continuous_endpoint_is_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))
    sessions = AsrSessions(RecordingStore(tmp_path / "archive"), AsrConfig(), FakeWire)
    app = build_asr_app(sessions)
    handlers = {fn.fn.__name__: fn.fn for fn in app.fns.values() if fn.fn}
    identity = new_development_session()
    try:
        sessions.begin(identity)
        assert not sessions.current(identity).stopped
        # The manual microphone path remains independent of the hidden VAD
        # segment submit event and still uses the explicit stop/save actions.
        assert "vad_segment" not in handlers
        assert not sessions.current(identity).stopped
    finally:
        sessions.release(identity)


def test_vad_pcm_segment_is_saved_exactly_and_transcribed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))
    wire = FakeWire()
    sessions = AsrSessions(RecordingStore(tmp_path / "archive"), AsrConfig(), lambda: wire)
    identity = new_development_session()
    app = build_vad_app(sessions)
    handlers = {fn.fn.__name__: fn.fn for fn in app.fns.values() if fn.fn}
    samples = np.arange(4000, dtype="<i2")
    payload = json.dumps(
        {
            "pcm16_b64": base64.b64encode(samples.tobytes()).decode("ascii"),
            "started_ms": 123.0,
            "events": [],
        }
    )
    try:
        updates = list(handlers["vad_segment"](payload, identity))
        assert updates[-1][1] == "Heute war ich im Park."
        assert any(update[2].startswith("VAD segment saved · 0.25 s") for update in updates)
        assert updates[-1][3]["frames"] == len(samples)
        result, duration = sessions.snapshot(identity)
        assert result.status == "completed"
        assert duration == pytest.approx(0.25)
        saved = sessions.current(identity).saved
        assert saved is not None and saved.audio_path.exists()
        restored, rate = __import__("soundfile").read(saved.audio_path, dtype="int16")
        assert rate == 16000
        assert np.array_equal(restored, samples)
        assert [event["type"] for event in wire.sent].count("input_audio_buffer.commit") == 1
        stages = {row["stage"] for row in updates[-1][8]}
        assert {
            "segment.event_received",
            "asr.audio_sent",
            "asr.commit_sent",
            "asr.completed",
        } <= stages
    finally:
        sessions.release(identity)


def test_vad_app_has_no_native_gradio_microphone_stream(tmp_path: Path) -> None:
    sessions = AsrSessions(RecordingStore(tmp_path / "archive"), AsrConfig(), FakeWire)
    app = build_vad_app(sessions)
    handlers = [fn.fn.__name__ for fn in app.fns.values() if fn.fn]
    assert "vad_segment" in handlers
    assert "append" not in handlers
    assert "begin" not in handlers


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps({"pcm16_b64": "!", "started_ms": 0, "events": []}),
        json.dumps(
            {"pcm16_b64": base64.b64encode(b"\0\0" * 100).decode(), "started_ms": 0, "events": []}
        ),
        json.dumps(
            {
                "pcm16_b64": base64.b64encode(b"\0\0" * 2000).decode(),
                "started_ms": float("nan"),
                "events": [],
            }
        ),
    ],
)
def test_invalid_vad_segments_are_rejected_without_opening_asr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))

    def forbidden_factory():
        pytest.fail("Invalid segment must not connect to ASR")

    sessions = AsrSessions(RecordingStore(tmp_path / "archive"), AsrConfig(), forbidden_factory)
    identity = new_development_session()
    app = build_vad_app(sessions)
    handlers = {fn.fn.__name__: fn.fn for fn in app.fns.values() if fn.fn}
    try:
        output = list(handlers["vad_segment"](payload, identity))
        assert output[0][2] == "VAD segment rejected or could not be processed"
        assert sessions.snapshot(identity)[0].status == "ready"
    finally:
        sessions.release(identity)


def test_voice_vad_segment_flows_through_asr_llm_and_tts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))
    from riva.audio.replies import ReplyConfig, ReplyManager, TextPart

    class Provider:
        def text(self, transcript, config):
            assert transcript == "Heute war ich im Park."
            yield TextPart("delta", "Das klingt schön.")
            yield TextPart("completed", "Das klingt schön.")

        def speech(self, phrase, config):
            assert phrase == "Das klingt schön."
            yield b"\0\0" * 12000

    wire = FakeWire()
    sessions = AsrSessions(RecordingStore(tmp_path / "archive"), AsrConfig(), lambda: wire)
    replies = ReplyManager(ReplyConfig(), Provider)
    identity = new_development_session()
    app = build_vad_app(sessions, replies)
    handlers = {fn.fn.__name__: fn.fn for fn in app.fns.values() if fn.fn}
    pcm = np.arange(4000, dtype="<i2")
    payload = json.dumps(
        {
            "pcm16_b64": base64.b64encode(pcm.tobytes()).decode("ascii"),
            "started_ms": 10.0,
            "events": [],
        }
    )
    try:
        updates = list(handlers["vad_segment"](payload, identity))
        assert updates[-1][1] == "Heute war ich im Park."
        assert updates[-1][4] == "Das klingt schön."
        assert updates[-1][6]["status"] == "completed"
        assert any(isinstance(update[5], bytes) for update in updates)
        assert any(row["stage"] == "reply.completed" for row in updates[-1][8])
    finally:
        replies.release(identity)
        sessions.release(identity)


def test_vad_self_test_uses_mocked_providers_and_allowlisted_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))
    from riva.audio.replies import ReplyConfig, ReplyManager
    from riva.experiments.vad_self_test import SyntheticReplyProvider, SyntheticWire

    sessions = AsrSessions(
        RecordingStore(tmp_path / "synthetic-archive"), AsrConfig(), SyntheticWire
    )
    replies = ReplyManager(ReplyConfig(), SyntheticReplyProvider)
    identity = new_development_session()
    app = build_vad_app(sessions, replies, test_mode=True)
    handlers = {fn.fn.__name__: fn.fn for fn in app.fns.values() if fn.fn}
    pcm = np.zeros(16000, dtype="<i2")
    payload = json.dumps(
        {
            "pcm16_b64": base64.b64encode(pcm.tobytes()).decode("ascii"),
            "started_ms": 50.0,
            "events": [{"stage": "harness.injected", "at_ms": 50.0}],
            "synthetic": True,
        }
    )
    try:
        updates = list(handlers["vad_segment"](payload, identity))
        assert "SYNTHETIC TEST" in updates[-1][2]
        assert updates[-1][1] == "Synthetischer Testlauf."
        assert updates[-1][4] == SyntheticReplyProvider.text_reply
        assert updates[-1][6]["status"] == "completed"
        stages = {row["stage"] for row in updates[-1][8]}
        assert {
            "harness.injected",
            "asr.audio_sent",
            "asr.commit_sent",
            "reply.completed",
        } <= stages
    finally:
        replies.release(identity)
        sessions.release(identity)


def test_voice_ui_stop_button_cancels_playback_and_resets_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))
    from riva.audio.replies import ReplyConfig, ReplyManager, TextPart

    class Provider:
        def text(self, transcript, config):
            yield TextPart("delta", "Hallo.")
            yield TextPart("completed", "Hallo.")

        def speech(self, phrase, config):
            yield b"\0\0" * 12000

    sessions = AsrSessions(RecordingStore(tmp_path / "archive"), AsrConfig(), FakeWire)
    replies = ReplyManager(ReplyConfig(), Provider)
    identity = new_development_session()
    app = build_asr_app(sessions, replies)
    handlers = {fn.fn.__name__: fn.fn for fn in app.fns.values() if fn.fn}
    try:
        sessions.begin(identity)
        sessions.append(audio(), identity)
        sessions.current(identity).stopped = True
        sessions.finish(identity)
        assert handlers["enable"](identity).interactive

        # Start reply
        reply_iter = handlers["reply"](identity, 100)
        first_step = next(reply_iter)
        assert not first_step[4].interactive  # button disabled
        assert first_step[5].interactive  # stop_button enabled

        # Consume one streamed update before cancellation. The stop control
        # must remain available while TTS audio is delivered.
        streamed_step = next(reply_iter)
        assert not streamed_step[4].interactive
        assert streamed_step[5].interactive

        # Stop via stop_reply handler
        # The generator may already have a queued audio update; cancellation
        # must make subsequent streamed updates inert as well.
        stop_out = handlers["stop_reply"](identity, 400)
        assert stop_out[1] is None  # speech audio cleared
        assert stop_out[2]["status"] == "cancelled"
        assert "abgebrochen" in stop_out[3]
        assert stop_out[4].interactive  # button re-enabled
        assert not stop_out[5].interactive  # stop_button disabled

        # Late playback attempts should not be recorded
        play_out = handlers["played"](identity, {}, 500)
        assert play_out[0]["first_browser_play_event_ms"] is None

        # Check telemetry
        logged = capsys.readouterr().out
        assert '"name": "playback.started"' not in logged
        assert '"name": "playback.stopped"' in logged
    finally:
        replies.release(identity)
        sessions.release(identity)
