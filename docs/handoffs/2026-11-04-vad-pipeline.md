# Handoff: Browser VAD segment pipeline

> **Superseded by `2026-11-05-vad-pipeline.md`.** The problems described below were resolved.

Date: 2026-11-04
Repository: `/home/rmary/projects/riva/riva-basic`
Beads: `riva-ojc` (active implementation), depends on `riva-0jw.5`.
Latest commit: `3ef3297 fix(asr): keep polling VAD diagnostics through ASR completion`.
Working tree was clean at handoff creation.

## Goal and intended architecture

Continuous mode is intended to use browser `MicVAD` as the *only* microphone
owner. On a VAD speech end, the browser converts its 16 kHz Float32 segment to
PCM16 and submits a bounded base64 payload through a hidden Gradio event. The
backend validates it, saves only that segment, runs one existing `LiveTurn`
OpenAI ASR connection/commit, and, in the voice app, sends a completed final
transcript to the existing `ReplyManager` for streamed LLM/TTS. Manual capture
remains a separate Gradio app.

This separation was added after native Gradio microphone streams and VAD's own
microphone competed for audio/encoder state. The old dual-input page emitted
`There is already an encoder stored which handles exactly the same mime types`
and frontend `Element is not in the DOM` errors. Do not merge the VAD input back
into the native Gradio microphone page without a deliberate transport redesign.

## Current entry points

- `riva-asr-test` at port 7860: manual native Gradio capture/streaming ASR.
- `riva-voice-test` at port 7861: VAD-only page with ASR and LLM/TTS replies.
- `riva-voice-test --vad-self-test --port 7862`: no-network synthetic harness,
  with mocked ASR/LLM/TTS and a **Run synthetic pipeline test** button.

The synthetic button calls the same backend segment-processing generator via a
separate Gradio event. It bypasses the VAD browser callback and hidden submit
button intentionally: it isolates backend/ASR/reply behavior from browser
segment handoff. It uses a generated 1-second tone and stores under
`../private/research-data/vad-self-test/`, not the normal voice-test archive.

Current servers were last verified HTTP 200 on all three ports. They may stop
when the coding session/server restarts; restart them explicitly, then verify
ports/HTTP before asking for browser retest. The user expects automatic restart
after code changes that require it.

## Latest reported behavior / unresolved issue

The user’s latest synthetic diagnostic timeline contained only:

```text
harness.injected
segment.event_received
segment.validated
asr.turn_accepted
asr.connection_starting
asr.session_ready
```

An earlier synthetic run created a synthetic recording/transcript and emitted
`recording.saved` and `asr.finalized` in `private/logs/vad-self-test.log`, while
the displayed timeline still stopped at `asr.session_ready`. It did not show
`asr.audio_sent`, `asr.commit_sent`, `asr.completed`, or reply stages. At that
time the synthetic harness still used the hidden submit route. The harness was
then changed to call the backend processor directly, and a subsequent change
added per-session diagnostic snapshots plus a 250 ms UI poll, but **the user has
not retested after those latest changes**. This is the immediate next step.

The 2026-11-04 23:50-ish test artifact in `private/research-data/vad-self-test`
was a synthetic test. Its transcript text is synthetic, not participant data.
No participant audio/transcript was read for this handoff.

If a fresh synthetic test still stops at `asr.session_ready`:

1. Inspect current ASR/voice/self-test logs and newest files only under the
   explicit synthetic `vad-self-test` path. Do not inspect real voice-test
   transcripts/audio.
2. Compare the diagnostic UI timeline with fixed-event log lines and the
   synthetic transcript metadata status.
3. The response startup guard is in `vad_segment_updates()` in
   `src/riva/experiments/asr_stream.py`: successful completed ASR should call
   `replies.start()` and emit `response.started`. In self-test mode the reply
   provider is `SyntheticReplyProvider` from
   `src/riva/experiments/vad_self_test.py`; no OpenAI call should occur.
4. The diagnostics are stored by `AsrSessions.update_vad_diagnostics()` and
   polled by `build_vad_app().poll_vad()`. Check whether polling runs after the
   generator finishes and whether the same timeline is retained.
5. If artifacts say ASR completed but there is no response-start event or reply
   artifact, determine why the reply branch was skipped or `ReplyManager.start`
   failed. Do not claim ASR→LLM→TTS is verified based on ASR artifacts alone.

If the synthetic harness now reaches all backend milestones but live VAD has no
`vad.speech_start` / `vad.speech_end` / `vad.submit_clicked` rows, investigate
the browser callback to hidden-button handoff. The selector currently supports
both DOM shapes: an element whose `#riva-vad-submit` itself is a button, or a
wrapper containing a button. Check actual browser DOM and network/Gradio events;
do not blindly change it back to one selector shape.

## Diagnostic model

The VAD page has a JSON timeline, updated both during generator updates and via
`poll_vad` every 250 ms. Expected stages:

- Browser: `vad.speech_start`, `vad.speech_end`, `vad.segment_queued`,
  `vad.submit_clicked` (or `harness.injected`).
- Backend: `segment.event_received`, `segment.validated`, `asr.turn_accepted`,
  `asr.connection_starting`, `asr.session_ready`, `asr.audio_sent`,
  `asr.commit_sent`, `asr.finalizing`, `asr.completed`/`asr.failed`,
  `recording.persisted`, `reply.generation_started`, `reply.completed`/failed.

Timeline values are allowlisted stage names, source, and elapsed milliseconds.
They contain no audio, transcription content, API errors, or prompts. ASR wire
send milestones are tracked by `LiveTurn.audio_sent` and `LiveTurn.commit_sent`
events in `src/riva/audio/transcription.py`.

## Important implementation files

- `src/riva/experiments/asr_stream.py`
  - `build_asr_app`: manual-only native Gradio microphone app.
  - `build_vad_app`: VAD-only UI, intentionally no native microphone component.
  - `vad_segment_updates`: payload validation, timeline recording, private
    segment persistence, ASR finalization, optional reply generation.
  - `VAD_BROWSER_JS`: pinned CDN scripts for `onnxruntime-web@1.14.0` and
    `@ricky0123/vad-web@0.0.27`; segment callback converts to PCM16.
- `src/riva/experiments/voice_reply.py`: launches VAD app and offers
  `--vad-self-test` mode.
- `src/riva/experiments/vad_self_test.py`: deterministic fake ASR wire and
  LLM/TTS provider; must never make paid/network provider calls.
- `src/riva/audio/transcription.py`: `LiveTurn`, session/WebSocket protocol,
  resampling and `audio_sent`/`commit_sent` milestones.
- `tests/test_transcription.py`: exact PCM persistence, rejection, ASR→LLM→TTS,
  synthetic timeline, and no-native-mic VAD app tests.

## Evidence and quality gates

Before the latest handoff changes, 90 tests passed; Ruff, mypy, pre-commit, and
`uv build` passed. The last committed changes added UI diagnostic polling and
synthetic callback coverage; rerun quality gates after any follow-up edits:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run pre-commit run --all-files
uv build
```

Browser verification is still pending. Unit tests do not prove MicVAD asset
loading, microphone permission, browser submit delivery, autoplay, or actual
speech endpointing. Do not close `riva-ojc` until the user verifies a real
segment on Windows Chrome/Edge and the timeline shows browser submission through
ASR completion; in the voice app also verify reply text/audio.

## Beads / current scope

- `riva-ojc`: active P1 implementation ticket for browser-VAD segment pipeline.
- `riva-0jw.5`: continuous listening/endpointing/interruption parent task, still
  open and in progress; native Gradio immediate interruption is not claimed.
- `riva-0jw.4`: closed with accepted limitation: server cancellation stops
  future generation/delivery; browser-buffered Gradio audio may continue briefly.
- `riva-0jw.3`: closed; manual streamed LLM/TTS baseline verified by user.
- `riva-99p`: voice quality/custom voices comparison.
- `riva-ddc`: Speech API migration ahead of announced 2027-01-06 shutdown.

Beads is the task tracker. Keep this handoff factual; update `riva-ojc` notes as
evidence arrives, and only close when browser acceptance criteria pass.
