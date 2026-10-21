import io
import json
import threading
import wave

import numpy as np
import pytest

from riva.audio.recordings import RecordingStore, new_development_session
from riva.audio.replies import (
    OpenAIReplyProvider,
    PhraseBuffer,
    ReplyConfig,
    ReplyManager,
    ReplyRun,
    TextPart,
    pcm_wav,
)


class FakeReplyProvider:
    def __init__(self, *, gated=False, failure=None):
        self.gated = gated
        self.failure = failure
        self.first_speech = threading.Event()
        self.continue_text = threading.Event()
        self.text_calls = 0
        self.speech_phrases = []

    def text(self, transcript, config):
        self.text_calls += 1
        yield TextPart("delta", "Das klingt gut. ")
        if self.gated:
            assert self.first_speech.wait(2)
            assert self.continue_text.wait(2)
        if self.failure == "text":
            raise RuntimeError("synthetic-private-provider-body")
        yield TextPart("delta", "Was hast du gemacht?")
        if self.failure != "missing_completion":
            final = (
                "wrong" if self.failure == "mismatch" else "Das klingt gut. Was hast du gemacht?"
            )
            yield TextPart("completed", final)

    def speech(self, phrase, config):
        self.speech_phrases.append(phrase)
        self.first_speech.set()
        if self.failure == "speech":
            raise RuntimeError("synthetic-private-provider-body")
        if self.failure == "odd_pcm":
            yield b"x"
            return
        marker = len(self.speech_phrases) * 1000
        pcm = np.full(10000, marker, dtype="<i2").tobytes()
        # Deliberately split at an odd byte boundary. The provider boundary is
        # not necessarily a complete PCM sample or an application audio frame.
        yield pcm[:10001]
        yield pcm[10001:]


def saved_turn(tmp_path):
    identity = new_development_session()
    return identity, RecordingStore(tmp_path).save(
        (24000, np.zeros(12000, dtype=np.int16)), identity
    )


def decode(wav):
    with wave.open(io.BytesIO(wav), "rb") as stream:
        assert stream.getframerate() == 24000
        assert stream.getnchannels() == 1
        assert stream.getsampwidth() == 2
        return stream.readframes(stream.getnframes())


def test_speech_starts_before_text_completion_and_audio_stays_ordered(tmp_path):
    _, saved = saved_turn(tmp_path)
    provider = FakeReplyProvider(gated=True)
    run = ReplyRun(saved, "Heute war ich im Park.", ReplyConfig(), provider, 100)
    chunks = []
    try:
        for _, wav, _ in run.updates():
            if wav:
                if not chunks:
                    assert not run.text_completed
                    provider.continue_text.set()
                chunks.append(decode(wav))
        assert run.status == "completed"
        assert provider.speech_phrases == ["Das klingt gut.", "Was hast du gemacht?"]
        expected = np.concatenate(
            [np.full(10000, 1000, dtype="<i2"), np.full(10000, 2000, dtype="<i2")]
        ).tobytes()
        assert b"".join(chunks) == expected
        assert decode((run.root / "speech.wav").read_bytes()) == expected
        assert run.snapshot()["first_text_ms"] is not None
        assert run.snapshot()["first_server_audio_ms"] is not None
        assert run.snapshot()["first_browser_play_event_ms"] is None
    finally:
        provider.continue_text.set()
        run.close()


@pytest.mark.parametrize(
    "failure,code",
    [
        ("text", "llm_failed"),
        ("speech", "tts_failed"),
        ("mismatch", "text_completion_mismatch"),
        ("missing_completion", "missing_text_completion"),
        ("odd_pcm", "invalid_pcm"),
    ],
)
def test_failures_are_terminal_sanitized_and_not_claimed_as_success(tmp_path, failure, code):
    _, saved = saved_turn(tmp_path)
    run = ReplyRun(
        saved, "Synthetischer Test.", ReplyConfig(), FakeReplyProvider(failure=failure), 0
    )
    try:
        list(run.updates())
        assert run.done.wait(2)
        assert run.status == "failed"
        assert run.error_code == code
        record = (run.root / "reply.json").read_text()
        assert "synthetic-private-provider-body" not in record
        assert json.loads(record)["status"] == "failed"
        assert saved.audio_path.exists()
    finally:
        run.close()


def test_browser_play_metric_is_distinct_from_server_audio_and_persists(tmp_path):
    _, saved = saved_turn(tmp_path)
    run = ReplyRun(saved, "Test.", ReplyConfig(), FakeReplyProvider(), 100)
    try:
        list(run.updates())
        run.record_playback(float("nan"))
        assert run.first_playback_ms is None
        assert run.record_playback(350) is True
        assert run.record_playback(900) is False
        assert run.first_playback_ms == 250
        data = json.loads((run.root / "reply.json").read_text())
        assert data["first_browser_play_event_ms"] == 250
        assert data["playback_metric"] == "browser_play_event_not_measured_audibility"
        for path in run.root.iterdir():
            assert path.stat().st_mode & 0o777 == 0o600
    finally:
        run.close()


def test_double_click_does_not_create_another_paid_generation(tmp_path):
    identity, saved = saved_turn(tmp_path)
    provider = FakeReplyProvider()
    manager = ReplyManager(ReplyConfig(), lambda: provider)
    try:
        first, created = manager.start(identity, saved, "Test.", 0)
        assert created
        list(first.updates())
        second, created = manager.start(identity, saved, "Test.", 10)
        assert second is first and not created
        assert provider.text_calls == 1
    finally:
        manager.release(identity)


def test_recording_from_another_session_is_rejected_before_provider_creation(tmp_path):
    _, saved = saved_turn(tmp_path)
    manager = ReplyManager(ReplyConfig(), lambda: pytest.fail("Must not create provider"))
    with pytest.raises(ValueError, match="does not belong"):
        manager.start(new_development_session(), saved, "Test.", 0)


def test_text_limit_stops_generation(tmp_path):
    _, saved = saved_turn(tmp_path)
    run = ReplyRun(saved, "Test.", ReplyConfig(max_chars=5), FakeReplyProvider(), 0)
    try:
        list(run.updates())
        assert run.error_code == "reply_text_limit"
    finally:
        run.close()


def test_phrase_buffer_avoids_decimal_splits_and_flushes_tail():
    buffer = PhraseBuffer()
    assert buffer.feed("Das kostet 3.") == []
    assert buffer.feed("50 Euro. Was ") == ["Das kostet 3.50 Euro."]
    assert buffer.feed("denkst du?") == []
    assert buffer.flush() == ["Was denkst du?"]


def test_phrase_buffer_bounds_punctuation_free_output():
    buffer = PhraseBuffer(limit=20)
    phrases = buffer.feed("eins zwei drei vier fünf sechs sieben acht") + buffer.flush()
    assert all(len(p) <= 20 for p in phrases)
    assert " ".join(phrases) == "eins zwei drei vier fünf sechs sieben acht"


def test_pcm_wav_rejects_incomplete_samples():
    with pytest.raises(ValueError):
        pcm_wav(b"x")


def test_format_metrics_renders_latency_and_error_without_free_text():
    from riva.experiments.voice_reply import format_metrics

    text = format_metrics(
        {
            "status": "completed",
            "first_text_ms": 1578.0,
            "first_server_audio_ms": 3088.5,
            "first_browser_play_event_ms": 2000.0,
            "audio_seconds_produced": 8.85,
        }
    )
    assert "**Status:** abgeschlossen" in text
    assert "Erster Text: 1.58 s" in text
    assert "Erstes Server-Audio: 3.09 s" in text
    assert "Text bis Audio: 1.51 s" in text
    assert "Wiedergabe im Browser: 2.00 s" in text
    assert "Erzeugte Audiodauer: 8.85 s" in text
    assert "Fehlercode" not in text
    assert "synthetic" not in text.lower()

    failed = format_metrics({"status": "failed", "error_code": "tts_failed"})
    assert "**Status:** fehlgeschlagen" in failed
    assert "**Fehlercode:** `tts_failed`" in failed
    assert "Erster Text: –" in failed


def test_provider_adapters_use_streaming_no_storage_and_pcm_without_network(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import openai

    requests = []

    @contextmanager
    def text_stream(**kwargs):
        requests.append(kwargs)
        yield iter(
            [
                SimpleNamespace(type="response.output_text.delta", delta="Hallo."),
                SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(status="completed", output_text="Hallo."),
                ),
            ]
        )

    @contextmanager
    def speech_stream(**kwargs):
        requests.append(kwargs)
        yield SimpleNamespace(iter_bytes=lambda chunk_size: iter([b"\0\0"]))

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0
            self.responses = SimpleNamespace(stream=text_stream)
            self.audio = SimpleNamespace(
                speech=SimpleNamespace(
                    with_streaming_response=SimpleNamespace(create=speech_stream)
                )
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(openai, "OpenAI", Client)
    provider = OpenAIReplyProvider()
    assert list(provider.text("Test.", ReplyConfig()))[-1].kind == "completed"
    assert list(provider.speech("Hallo.", ReplyConfig())) == [b"\0\0"]
    assert requests[0]["store"] is False
    assert requests[1]["response_format"] == "pcm"
