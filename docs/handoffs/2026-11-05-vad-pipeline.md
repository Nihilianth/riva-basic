# Handoff: Browser VAD voice pipeline (verified)

Date: 2026-11-05
Repository: `/home/rmary/projects/riva/riva-basic`
Supersedes: `2026-11-04-vad-pipeline.md` (its "unresolved synthetic timeline" problem is fixed).
Status: **live-verified by the user in Windows Chrome** on `http://localhost:7861`. Experimental,
not participant-ready. Edge, Firefox and speaker echo were not tested.

Closed this session: `riva-ojc` (pipeline), `riva-uxm` (second reply silent), `riva-ebq`
(cancel in-flight reply), `riva-b9r` (hold/merge), `riva-0jw.5` (continuous listening).
Facts about *why* things work are in those tickets' notes; this file is the map.

## What it does

Continuous voice mode: the browser's `MicVAD` is the only microphone owner. Each detected
speech segment becomes bounded 16 kHz PCM16, goes to the backend, is validated and privately
saved, transcribed (OpenAI realtime ASR), turned into an LLM reply and spoken (OpenAI TTS).

Entry points (all via `uv run`, private data under `../private/`):

| Port | Command | Purpose |
|---|---|---|
| 7860 | `riva-asr-test --private-dir <p> --port 7860` (needs `--env-file .env`) | manual native-Gradio recording; unchanged, still uses Gradio's audio streaming |
| 7861 | `riva-voice-test --private-dir <p> --port 7861` (needs `--env-file .env`) | VAD-only voice app; real ASR/LLM/TTS |
| 7862 | `riva-voice-test --vad-self-test --private-dir <p> --port 7862` | no-network synthetic harness (mock ASR/LLM/TTS), **backend only** |

Servers stop with the coding session; restart them after code changes and verify HTTP and
`ss -ltn` for 7860-7862. `private/logs/{asr-test,voice-test,vad-self-test}.log` are overwritten
on every restart.

## Data flow (7861)

1. **Browser VAD** (`VAD_BROWSER_JS` in `src/riva/experiments/asr_stream.py`; vad-web 0.0.27 +
   onnxruntime-web 1.14 from jsDelivr; `redemptionMs` 900, `preSpeechPadMs` 300, `minSpeechMs` 250).
   - `onSpeechStart` sets `window.rivaVad.speaking` and suspends reply audio.
   - `onSpeechRealStart` (confirmed, not a misfire) → hidden control `#riva-vad-speech`
     sends `{kind:"start", at}` (browser clock).
   - `onSpeechEnd` → `{kind:"end", at}`, drops the stale reply audio, queues the segment
     (pending cap 4; a drop is reported as `browser.segment dropped_busy`), clicks the hidden
     `#riva-vad-submit` button which sends the JSON payload.
   - `onVADMisfire` resumes suspended reply audio.
2. **Backend segment handler** `vad_segment_updates()` (generator; queued, `concurrency_limit=1`):
   validates the payload, saves exactly that segment, runs one `LiveTurn` ASR connection, records
   the diagnostics timeline, then decides whether to start a reply.
3. **Speech state** (`AsrSessions.speech`, per session): `speaking`, `last_start_ms`, `held`
   transcripts; events are ordered by the browser clock; `speaking` expires after 35 s.
   - `vad_speech` handler (unqueued): records start/end; on `start` calls
     `ReplyManager.cancel_generating()` (cancels a reply only while it is still generating).
4. **Gate** (in `vad_segment_updates`, before `replies.start`): if the user is speaking, or a
   speech start was reported after this segment's `vad.speech_end`, the transcript is **held**
   (`response.held`, timeline `reply.held`). Otherwise `held + new` are merged by
   `merge_transcripts()` into **one** LLM turn. A reply cancelled before it was heard puts its
   text back into `held`; a reply that was already heard does not.
5. **Reply** (`src/riva/audio/replies.py`, `ReplyRun`): streamed LLM text, phrase-wise TTS, PCM
   accumulated in `run.pcm` (24 kHz mono s16le). Limits: 1200 chars, 45 s audio, 60 s wall time.
6. **Playback**: `GET /riva/reply_audio/<generation_id>` (in `voice_reply.py`,
   `add_reply_audio_route`) streams the PCM; the page's `AudioContext` (24 kHz) schedules it
   gaplessly. The generation id reaches the page through the hidden `#riva-generation-id` textbox.
   Barge-in = `ctx.suspend()` on speech start, stop/abort when the segment ends, resume on misfire.
   First audible chunk posts `/riva/playback` (browser playback timing; this route had been
   returning 422 until 2026-11-05).

UI (7861): "Browser VAD:" stage line (`#riva-vad-state`), diagnostics timeline, transcript of the
**last segment**, **"An das LLM gesendeter Text (zusammengeführt)"** (the merged text actually sent;
UI only, never logged), segment status, reply text and metrics.

## Diagnostics and counting calls

Stage line examples: `listening`, `speech start`, `speech confirmed`, `speech end, segment queued`,
`audio ctx_running`, `audio stream_started`, `audio first_chunk`, `audio ended`,
`audio dropped_user_speaking`, `audio suspended_user_speaking`, `audio resumed_misfire`,
`FAILED: <ErrorName>`.

Server log lines (JSON, ids and short tokens only; `browser.*` events come from
`POST /riva/browser_event`, allowlisted, `session_id` is the literal `"browser"`):
`recording.saved`, `asr.finalized`, `response.started|completed|failed|cancelled|held`,
`tts.requested`, `playback.event|started`, `browser.audio`, `browser.hls_error`, `browser.segment`.

Per-run cost recipe (run after a test, before the next restart):

```sh
cd ../private/logs
for e in asr.finalized response.started response.held response.cancelled tts.requested \
         playback.started browser.segment; do printf '%-20s %s\n' $e "$(grep -c "\"$e\"" voice-test.log)"; done
```

ASR calls = `asr.finalized` (+ `provider.failed`); LLM calls = `response.started`; TTS calls =
`tts.requested`; LLM calls saved by the gate ≈ `response.held`. Measured on one 4-fragment
utterance: 4 ASR, 3 held, 1 LLM, 2 TTS. ASR is not reduced by any gate (each segment is still
transcribed, ~4-5 s final latency).

## Gotchas learned (do not rediscover)

- A `gr.Button(visible=False)` is **not in the DOM**; use `visible="hidden"` for controls the page JS clicks.
- A generator event registered with `queue=False` returns only its first `yield`.
- Yielding `None` to a streaming `gr.Audio` opens an empty HLS playlist (hls.js fatal
  `levelEmptyError`); Gradio also recreates hls.js per stream and autoplays only once. That is why
  VAD replies use the PCM endpoint + Web Audio, not Gradio audio. (The manual app still uses Gradio audio.)
- With `from __future__ import annotations`, import FastAPI's `Request` at module level or the route
  treats `request` as a query parameter (422).
- The synthetic harness (7862) bypasses the browser by design: it proves the backend, not the
  browser handoff, VAD, autoplay or audio quality.
- No headless browser is installed; browser behavior can only be verified by the user in Chrome.

## Known limits / not verified

- **Echo**: AI voice leaking into the microphone on speakers is untested (use headphones for now).
- Edge/Firefox, mobile and multi-user are untested; endpoints have no per-user authorization
  (local prototype; same as `/riva/playback`).
- A held transcript is answered only when a later segment arrives; there is no timer flush.
- Merging is plain concatenation; there is no conversation history across turns.
- Cancelling stops *future* LLM/TTS work; a provider call already in flight is only joined with a
  short timeout.
- `redemptionMs` is hard-coded (see `riva-asn`).

## Tests and gates

`uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest &&
uv run pre-commit run --all-files && uv build`. 104 tests passed at the last run. Unit tests cover
validation, exact PCM persistence, ASR→LLM→TTS ordering, synthetic harness, hold/merge/restore,
cancel semantics, the PCM endpoint and event-schema rules; none of them prove browser behavior.

## Open tickets (all lower priority)

`riva-asn` user-adjustable pause threshold (P2); `riva-508` audio-merge evaluation (P4);
`riva-ddc` Speech API migration, **deadline 2027-01-06**; `riva-99p` voice quality; epic `riva-0jw`
(`.6`, `.7`, `.9`); `riva-y7s` backup/recovery; `riva-1y0` OpenMemory; `riva-4m6` accounts.
`src/riva/experiments/demo_1_notes.md` is the user's untracked demo note; do not commit it unasked.

## Privacy reminders

Never read `.env`, participant recordings or real transcripts/replies under
`../private/research-data/voice-test/`. The synthetic archive under `vad-self-test/` is test data.
Logs carry event names and opaque ids only.
