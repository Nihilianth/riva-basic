import stat
import threading
import time

import pytest
from fastapi.testclient import TestClient
from support.demo import GatedSynth

from riva.demo.app import build_demo_api, build_runtime
from riva.demo.config import load_config
from riva.demo.fakes import EchoTurnLLM, FakeProbe
from riva.demo.preflight import voice_cache_check
from riva.demo.providers import Providers
from riva.demo.voices import VoiceError, VoicePreviews
from riva.experiments.vad_self_test import SyntheticWire

CONFIG = load_config()


class CountingSynth:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def speech(self, phrase, config):
        self.calls.append((config.voice, phrase))
        if self.fail:
            raise RuntimeError("sk-secret provider body")
        time.sleep(0.05)
        yield b"\x01\x00" * 24000


def test_a_preview_is_generated_once_and_then_served_from_disk(tmp_path) -> None:
    synth = CountingSynth()
    previews = VoicePreviews(tmp_path / "cache", synth, CONFIG)
    assert not previews.cached("coral") and previews.cached_count() == 0
    first = previews.get("coral")
    again = previews.get("coral")
    assert first == again and first.read_bytes()[:4] == b"RIFF"
    assert len(synth.calls) == 1 and previews.generated == 1 and previews.cached("coral")
    assert synth.calls[0] == ("coral", CONFIG.preview_sentence)  # the same sentence for every voice
    assert stat.S_IMODE(first.stat().st_mode) == 0o600 or first.stat().st_mode & 0o077 == 0


def test_concurrent_requests_for_one_voice_cost_one_synthesis(tmp_path) -> None:
    synth = CountingSynth()
    previews = VoicePreviews(tmp_path / "cache", synth, CONFIG)
    threads = [threading.Thread(target=previews.get, args=("sage",)) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(synth.calls) == 1 and previews.generated == 1


def test_a_changed_model_or_sentence_makes_a_new_file_instead_of_serving_an_old_one(
    tmp_path,
) -> None:
    from dataclasses import replace

    synth = CountingSynth()
    old = VoicePreviews(tmp_path / "cache", synth, CONFIG).get("coral")
    other = replace(CONFIG, preview_sentence="Hallo! Das ist ein anderer Satz.")
    new = VoicePreviews(tmp_path / "cache", synth, other).get("coral")
    assert old != new and len(synth.calls) == 2


def test_unknown_voices_and_provider_failures_are_rejected_safely(tmp_path) -> None:
    previews = VoicePreviews(tmp_path / "cache", CountingSynth(fail=True), CONFIG)
    for bad in ("robot", "../../etc/passwd", ""):
        with pytest.raises(ValueError):
            previews.get(bad)  # rejected before any provider call
    assert previews.synth.calls == []  # type: ignore[attr-defined]
    with pytest.raises(VoiceError) as error:
        previews.get("coral")
    assert "secret" not in repr(error.value) and not previews.cached("coral")
    assert list((tmp_path / "cache").glob(".writing-*")) == []


def test_prepare_all_needs_confirmation_and_only_generates_missing_voices(tmp_path) -> None:
    synth = CountingSynth()
    previews = VoicePreviews(tmp_path / "cache", synth, CONFIG)
    previews.get("coral")
    with pytest.raises(PermissionError):
        previews.prepare_all(confirmed=False)
    assert len(synth.calls) == 1
    made, had = previews.prepare_all(confirmed=True)
    assert (made, had) == (len(CONFIG.voices) - 1, 1) and len(synth.calls) == len(CONFIG.voices)
    assert previews.prepare_all(confirmed=True) == (0, len(CONFIG.voices))


def test_the_cache_status_is_a_preflight_hint() -> None:
    assert voice_cache_check(10, 10).status == "ok"
    check = voice_cache_check(3, 10)
    assert check.status == "warn" and "3 von 10" in check.detail


def test_the_preview_endpoint_serves_wav_and_rejects_unknown_voices(tmp_path) -> None:
    synth = GatedSynth()
    synth.release.set()
    runtime = build_runtime(
        CONFIG,
        Providers(EchoTurnLLM(), synth, SyntheticWire),
        tmp_path / "archive",
        probe=FakeProbe(),
    )
    client = TestClient(build_demo_api(runtime))
    response = client.get("/riva/voice_preview/coral")
    assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
    assert response.content[:4] == b"RIFF" and synth.phrases == [CONFIG.preview_sentence]
    assert client.get("/riva/voice_preview/coral").status_code == 200
    assert synth.phrases == [CONFIG.preview_sentence]  # second request came from the cache
    assert client.get("/riva/voice_preview/robot").status_code == 404
    assert client.get("/riva/voice_preview/..%2Fsecret").status_code == 404


def test_a_preview_synthesis_is_counted_in_the_log_with_its_voice(tmp_path, capsys) -> None:
    import json

    previews = VoicePreviews(tmp_path / "cache", CountingSynth(), CONFIG)
    previews.get("echo")
    previews.get("echo")  # cached: no second request, no second log line
    previews.get("alloy")
    events = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    assert [(e["name"], e["detail"]) for e in events] == [
        ("tts.requested", "echo"), ("tts.requested", "alloy"),
    ]  # fmt: skip
    assert CONFIG.preview_sentence not in json.dumps(events)  # content-free
