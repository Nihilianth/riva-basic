import asyncio
import os
from pathlib import Path

import numpy as np
import pytest

from riva.audio.recordings import RecordingStore, new_development_session
from riva.experiments.audio_capture import build_app, prepare_environment


def test_prepare_environment_uses_private_gradio_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRADIO_TEMP_DIR", "unused")
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "True")
    root = prepare_environment(tmp_path / "private")
    assert os.environ["GRADIO_TEMP_DIR"] == str(root / "gradio-cache")
    assert os.environ["GRADIO_ANALYTICS_ENABLED"] == "False"
    assert (root / "gradio-cache").stat().st_mode & 0o777 == 0o700


def test_gradio_app_and_callback_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))
    app = build_app(RecordingStore(tmp_path / "recordings"))
    config = app.get_config_file()
    assert config["analytics_enabled"] is False
    handler = next(fn.fn for fn in app.fns.values() if fn.fn and fn.fn.__name__ == "save_recording")
    path, metadata, status = handler(
        (16000, np.zeros(16000, dtype=np.int16)), new_development_session()
    )
    assert Path(path).exists()
    assert metadata["duration_seconds"] == 1
    assert status == "Aufnahme gespeichert."
    logged = capsys.readouterr().out
    assert '"name": "recording.saved"' in logged
    assert str(tmp_path) not in logged

    # Exercise routing/configuration without opening a listening socket. This
    # deliberately does not claim microphone or real browser playback coverage.
    from fastapi.testclient import TestClient

    with TestClient(app.app) as client:
        response = client.get("/config")
    assert response.status_code == 200
    assert any(c["type"] == "audio" for c in response.json()["components"])


def test_callback_rejects_missing_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    import gradio as gr

    app = build_app(RecordingStore(tmp_path / "recordings"))
    handler = next(fn.fn for fn in app.fns.values() if fn.fn and fn.fn.__name__ == "save_recording")
    with pytest.raises(gr.Error):
        handler(None, new_development_session())


def test_playback_copy_passes_launched_file_checks_without_exposing_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))
    from fastapi.testclient import TestClient
    from gradio.context import LocalContext
    from gradio.exceptions import InvalidPathError
    from gradio.processing_utils import async_move_files_to_cache
    from gradio.state_holder import SessionState

    # pytest's directory is inside /tmp, which Gradio normally permits. Simulate
    # the real layout where the archive is outside both cwd and system temp.
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path / "system-temp"))
    store = RecordingStore(tmp_path / "archive")
    app = build_app(store)
    block_fn = next(fn for fn in app.fns.values() if fn.fn and fn.fn.__name__ == "save_recording")
    identity = new_development_session()
    prediction = block_fn.fn((16000, np.zeros(16000, dtype=np.int16)), identity)
    cached_path, metadata, _ = prediction
    original = (
        store.root / identity.user_id / identity.session_id / metadata["turn_id"] / "recording.wav"
    )
    assert original.exists()
    assert Path(cached_path).is_relative_to(tmp_path / "cache")
    assert Path(cached_path) != original
    assert Path(cached_path).read_bytes() == original.read_bytes()
    assert original.stat().st_mode & 0o777 == 0o600
    assert Path(cached_path).stat().st_mode & 0o777 == 0o600
    assert app.allowed_paths == []

    playback = next(
        c
        for c in app.blocks.values()
        if getattr(c, "label", None) == "Gespeicherte Aufnahme anhören"
    )
    app.has_launched = True
    token = LocalContext.blocks.set(app)
    try:
        # This would reproduce the user's InvalidPathError with the old return value.
        with pytest.raises(InvalidPathError):
            asyncio.run(async_move_files_to_cache(playback.postprocess(str(original)), playback))
        output = asyncio.run(app.postprocess_data(block_fn, prediction, SessionState(app)))
    finally:
        LocalContext.blocks.reset(token)

    with TestClient(app.app) as client:
        response = client.get(output[0]["url"])
        assert response.status_code == 200
        assert response.content == original.read_bytes()
        assert client.get(f"/gradio_api/file={original}").status_code == 403
        assert (
            client.get(f"/gradio_api/file={original.parent / 'metadata.json'}").status_code == 403
        )


def test_playback_copy_failure_keeps_original_and_reports_saved_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "cache"))
    import gradio as gr

    store = RecordingStore(tmp_path / "archive")
    app = build_app(store)
    playback = next(
        c
        for c in app.blocks.values()
        if getattr(c, "label", None) == "Gespeicherte Aufnahme anhören"
    )

    def fail(*args: object) -> None:
        raise OSError("synthetic cache write failure")

    monkeypatch.setattr(playback, "move_resource_to_block_cache", fail)
    handler = next(fn.fn for fn in app.fns.values() if fn.fn and fn.fn.__name__ == "save_recording")
    with pytest.raises(gr.Error, match="Aufnahme gespeichert"):
        handler((16000, np.zeros(16000, dtype=np.int16)), new_development_session())
    assert len(list(store.root.glob("*/*/*/recording.wav"))) == 1
