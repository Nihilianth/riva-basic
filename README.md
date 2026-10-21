# RIVA

Reflective Interactive Voice Assistant: an architecture prototype for accessible,
personalized German-language journaling. Local capture and German streaming ASR
experiments are available; orchestration and TTS are not implemented yet.

## Try streaming German transcription

Set `OPENAI_API_KEY` in the ignored `.env` file, then run:

```sh
uv sync --locked
uv run --env-file .env riva-asr-test
```

Open **http://localhost:7860** in Windows Chrome/Edge. Unlike the capture-only test,
**this sends microphone audio to OpenAI while recording and incurs API costs**.
Starting/visiting the UI alone does not connect to OpenAI.

Record a short German sentence, leave a short pause after the last word, stop the
microphone, and click **Abschließen & speichern**. Provisional text appears while
recording; final text appears after explicit commit. Audio + `transcript.txt` +
`transcript.json` + metadata are saved under `../private/research-data/asr-test/`.
Failed transcriptions preserve the received audio and a failed JSON record, never
mislabel partial text as final. Use the discard button only if you want to discard
the current unsaved input. Refreshing/disconnecting does not automatically save it.

**Known transport limitation:** native Gradio streaming may discard the last unsent
chunk on stop. The archive contains the received mono stream, not a guaranteed
complete browser recording. Compare the replay with what you said during testing.
There are no trustworthy word timestamps or SRT exports in this step.

Default model: `gpt-live-transcribe`, German hint, low transcription delay, one
WebSocket connection per turn, stateful resampling to mono PCM16 at 24 kHz. Model
access depends on your OpenAI project. `--model gpt-transcribe` is an alternative
that starts transcription after commit rather than providing live captions.
`--delay minimal` can be evaluated for the live model; measure accuracy as well as
latency. No automatic retries or silent model fallback are performed.

Only manual turn completion is currently supported. Continuous listening,
endpointing, assistant replies and interruption are later tasks. Provider activity
expires after 20 seconds without new audio; a turn is limited to 120 seconds.
This remains a loopback-only, unauthenticated development tool, not a participant app.

## Try the microphone experiment

From this checkout in WSL:

```sh
uv sync --locked
uv run riva-audio-test
```

Open **http://localhost:7860** in Windows Chrome/Edge. Allow microphone access,
record, stop, then press **Aufnahme speichern**. Listen to the saved recording
and inspect its sample rate, channels, duration, and opaque IDs.

No API key is needed and no audio is sent to OpenAI. PCM16 WAV + JSON metadata
are saved under `../private/research-data/audio-test/`. Temporary Gradio audio
copies are stored under `../private/gradio-cache/`. Both must stay outside Git.
Only the selected recording's playback copy is served; the recording archive is
not added to Gradio's allowed file paths.
The recording limit defaults to 120 seconds. Clearing the widget does not delete
persisted recordings.

```sh
uv run riva-audio-test --private-dir /absolute/private/path --port 7860 --max-seconds 60
```

This is a **local-only, unauthenticated development tool**, not the participant
application. It binds to loopback and does not enable public Gradio sharing.
If WSL localhost forwarding requires binding to all interfaces, use `--host 0.0.0.0`
only on a trusted network with appropriate firewall rules. This can expose the
unauthenticated app to other machines. Microphone access from another computer
normally requires trusted HTTPS, unlike localhost.

FFmpeg must be available **inside WSL** for audio formats that need conversion.
Verify with `ffmpeg -version`; a Windows installation alone may not be visible
to the Linux process. Automated tests do not prove Windows microphone behavior.

## Development

Use Linux tools in WSL and edit this checkout on the Linux filesystem. Windows
Chrome/Edge access the Gradio experiment through localhost.

Prerequisites: Git, uv (bootstrap tested with 0.13.1), Python 3.12, and Beads
(`bd`, bootstrap tested with 1.3.1 embedded Dolt). Hook setup needs network access;
pre-commit can provision the Go toolchain used to build the pinned Gitleaks hook.

```sh
uv sync --locked
uv run pre-commit install
bd hooks install --chain
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run pre-commit run --all-files
```

Install pre-commit **before** Beads hooks. Beads preserves existing hook content;
verify both remain installed after upgrades. See [development workflow](docs/development.md).

The build backend is `uv_build`; uv manages dependencies and the environment.
Build a wheel and source distribution with `uv build`. Commit `uv.lock`.

## Workspace

```text
riva/                         # workspace only, no parent Git repository
├── riva-basic/               # this Git repository + uv package
│   ├── src/riva/             # application and development utilities
│   ├── tests/fixtures/       # synthetic data only
│   └── docs/                # reviewed discussions, decisions, experiments
├── private/                  # NOT in Git; restricted and backed up separately
│   ├── agent-sessions/
│   ├── beads-backups/
│   └── research-data/
└── infrastructure/           # optional separate repository later
```

Independent future uv tools may get sibling repositories. Multiple executable
targets alone do not require splitting the application repository.

## Project memory

- **Tasks:** Beads; start with `bd prime` and `bd ready`.
- **Accepted decisions:** [decision records](docs/decisions/0001-development-foundation.md).
- **Requirements and discussion:** [initial protocol](docs/discussions/2026-10-21-project-foundation.md).
- **Current experiment:** [Gradio audio baseline](docs/experiments/gradio-audio-baseline.md).
- **Raw OpenCode sessions:** private exports, not committed; see [archiving](docs/development.md#session-archives).
- **OpenMemory:** optional local development memory; not installed or connected yet.
  See [memory boundaries](docs/development-memory.md).

## Data safety

Never commit credentials, recordings, participant transcripts, raw agent exports,
or production logs. `.env.example` documents future configuration; the current
utilities do not load `.env` automatically. Unit tests use synthetic data and
mocked commands; paid live tests require explicit opt-in.

Git identity is repository-local: `Nihilianth <nihili4nth@gmail.com>`.
No GitHub remote is configured yet. A local checkout or backup in the same WSL
installation is not an independent backup.
