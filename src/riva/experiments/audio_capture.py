"""Local-only native Gradio microphone capture. No OpenAI calls or credentials."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from riva.audio.recordings import (
    AudioInput,
    RecordingStore,
    SessionIdentity,
    new_development_session,
    private_directory,
)
from riva.event_log import Event, EventName, write_event

if TYPE_CHECKING:
    import gradio as gr


def prepare_environment(private_root: Path) -> Path:
    root = private_directory(private_root)
    cache = private_directory(root / "gradio-cache")
    # Set before importing Gradio; never use a shared system cache for recordings.
    os.environ["GRADIO_TEMP_DIR"] = str(cache)
    os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
    return root


def build_app(store: RecordingStore) -> gr.Blocks:
    import gradio as gr

    def save_recording(
        audio: AudioInput | None,
        identity: SessionIdentity,
    ) -> tuple[str, dict[str, Any], str]:
        if audio is None:
            raise gr.Error("Bitte zuerst eine Aufnahme machen und stoppen.")
        try:
            saved = store.save(audio, identity)
        except (ValueError, OSError, RuntimeError):
            # Keep paths, samples and exception bodies out of browser/log output.
            raise gr.Error("Speichern fehlgeschlagen. Aufnahme und Speicherplatz prüfen.") from None
        write_event(
            Event(
                EventName.RECORDING_SAVED,
                identity.session_id,
                saved.metadata.turn_id,
                duration_ms=saved.metadata.duration_seconds * 1000,
            ),
            sys.stdout,
        )
        try:
            # Publish only a copy through this component's managed cache. Returning
            # the archive path fails Gradio's file-access check after launch; adding
            # the archive root to allowed_paths would expose unrelated recordings.
            cached_path = playback.move_resource_to_block_cache(saved.audio_path)
            if cached_path is None or not Path(cached_path).resolve().is_relative_to(
                Path(playback.GRADIO_CACHE).resolve()
            ):
                raise ValueError("Playback copy was not placed in the component cache")
        except (ValueError, OSError, RuntimeError):
            raise gr.Error(
                "Aufnahme gespeichert, aber die Wiedergabe konnte nicht vorbereitet werden."
            ) from None
        return cached_path, asdict(saved.metadata), "Aufnahme gespeichert."

    app = gr.Blocks(analytics_enabled=False, delete_cache=(300, 3600))
    with app:
        gr.Markdown(
            "# RIVA – Mikrofontest\n"
            "**Nur lokaler Entwicklungstest, keine Teilnehmerkonten.**\n\n"
            "1. Mikrofon erlauben und aufnehmen. 2. Aufnahme stoppen. "
            "3. **Aufnahme speichern** wählen.\n\n"
            "Audio wird lokal gespeichert, nicht an OpenAI gesendet. "
            "Noch keine Transkription oder kontinuierliche Spracherkennung."
        )
        identity = gr.State(value=new_development_session)
        microphone = gr.Audio(
            sources=["microphone"],
            type="numpy",
            format="wav",
            label="Mikrofonaufnahme",
            editable=False,
            buttons=[],
        )
        save = gr.Button("Aufnahme speichern", variant="primary")
        status = gr.Textbox(label="Status", interactive=False)
        playback = gr.Audio(
            label="Gespeicherte Aufnahme anhören",
            type="filepath",
            interactive=False,
            autoplay=False,
            buttons=[],
        )
        details = gr.JSON(label="Technische Aufnahmedaten")
        gr.Markdown(
            "Das Löschen im Mikrofonfeld löscht **keine gespeicherten Dateien**. "
            "Gespeicherte Aufnahmen bleiben bis zur manuellen Löschung erhalten. "
            "Temporäre Browser-/Serverkopien können zusätzlich bestehen bleiben."
        )
        save.click(
            save_recording,
            inputs=[microphone, identity],
            outputs=[playback, details, status],
            api_visibility="private",
            concurrency_limit=1,
        )
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--private-dir",
        type=Path,
        default=Path(os.environ.get("RIVA_PRIVATE_DIR", "../private")),
        help="Private storage outside Git (default: RIVA_PRIVATE_DIR or ../private)",
    )
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--max-seconds", type=float, default=120)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    try:
        root = prepare_environment(args.private_dir)
        store = RecordingStore(
            root / "research-data" / "audio-test", max_duration_seconds=args.max_seconds
        )
    except (ValueError, OSError):
        parser.error("Invalid private storage directory or recording limit")
    if args.host == "0.0.0.0":
        print("WARNING: no authentication; binding all interfaces is for trusted development only.")
    print(f"Audio test: http://localhost:{args.port} (no OpenAI requests)")
    app = build_app(store)
    app.launch(
        server_name=args.host,
        server_port=args.port,
        share=False,
        inbrowser=False,
        show_error=False,
        max_file_size="30mb",
        quiet=True,
    )
