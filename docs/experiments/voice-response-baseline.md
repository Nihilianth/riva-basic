# Manual voice response baseline

Date: 2026-10-22. Beads: `riva-0jw.3`, `riva-0jw.4`.
Status: implemented and tested offline; live LLM/TTS, browser playback and cancellation verification pending.

## Scope and workflow

`uv run --env-file .env riva-voice-test` serves localhost:7861. It reuses native
Gradio streaming ASR, private recording storage and delivery replay protection.
After Stop and **Abschließen & speichern**, review final text and press **Antwort
erzeugen**. This explicit step enables checking ASR before incurring LLM/TTS costs.
Nothing automatically creates an entry or accepts provisional ASR as final.

One current-turn-only German reply is requested through the OpenAI Responses API
(`store=False`, no automatic retries). The instruction requests at most two short
sentences and one concrete follow-up without inventing experiences or emotions.
This is not LangGraph, a therapeutic intervention, or a participant-ready assistant.

## Streaming and persistence

The text producer and speech synthesizer run concurrently. A phrase buffer starts
synthesis at sentence/size boundaries without waiting for the complete reply.
Phrases are synthesized sequentially to preserve speech order. Raw mono 24 kHz
PCM16 is reassembled across arbitrary network byte boundaries, framed as independent
WAV chunks and yielded to Gradio's streaming audio component. Gradio handles browser
delivery/encoding; this additional buffering still needs latency measurement.

Queues are bounded (four phrases/eight audio frames), output is limited to 1,200
characters and 45 seconds of generated audio, and a 60-second cooperative deadline
limits generation. Provider reads time out after 30 seconds; cooperative cleanup
can wait for a blocked read. This is not immediate API or browser-buffer cancellation;
the next cancellation/interruption task must address that explicitly.

Text completion is checked against streamed text. An incomplete/mismatched response,
provider failure, invalid PCM or storage failure is not labelled successful. Partial
text/audio may already have reached the browser; they cannot be retroactively
retracted. A generated speech file means synthesized audio, not confirmed playback.
Streaming content is not fully reviewable before speech; use synthetic development
examples, not participant sessions, while these limitations remain.

Each generation has a UUID, belongs to the saved user/session/turn, and is stored
outside Git under the input turn's `replies/<id>/` directory. Reply JSON records
status, model/voice settings, text-completion flag, requested speech phrases and
metrics. Errors use fixed codes, never raw provider exception bodies. Repeated
clicks for the same turn reuse its existing generation rather than triggering new
requests. A new turn creates a new generation; there is no silent retry/fallback.

## Latency semantics

- `first_text_ms`: server reply-run start → first nonempty model text delta.
- `first_server_audio_ms`: server reply-run start → first PCM audio frame produced.
- `first_browser_play_event_ms`: browser reply-request timestamp → first Play event.
  It includes request/queue/delivery buffering but not earlier ASR/manual review.

Browser timestamps come from `performance.now()` on the same page, not from mixing
wall-clock/server/client clocks. A Play event is attributed to the session's
current reply generation and only the first event is stored. The event can arrive
before the JSON panel update, so the handler does not require the panel to already
contain the generation ID. A delayed Play from a previous turn's still-visible
player could therefore be attributed to the current run; treat the value as a
proxy and verify first-audible latency separately.
An absent Play event remains null; replay/resume does not overwrite the first event.
The JavaScript observer is retained for diagnostics, but Gradio streamed audio did
not emit a usable browser play signal in the tested browsers; the browser field
may remain null. Use `first_server_audio_ms` plus manual timing for latency decisions.
The Play event is only a proxy: a muted device, stalled buffer or hardware latency
means it is not proof of first audible sound. Measure audible latency separately.

## Model lifecycle

`gpt-4.1-mini` is the low-latency, non-reasoning text baseline. Dedicated speech uses
`gpt-4o-mini-tts` with `coral`. Current OpenAI docs mark Speech API models deprecated,
with shutdown January 6, 2027. `riva-ddc` tracks migration to a supported workflow
while preserving the separate text orchestration/TTS boundary and checking fidelity.
The recommended Realtime model cannot simply be supplied to `/v1/audio/speech`.

## Playback cancellation and stale generation handling (riva-0jw.4)

### Mechanics
- **Stop button and queue bypass:** An explicit **Wiedergabe abbrechen** button triggers
  cancellation without queuing (`queue=False`), avoiding head-of-line blocking behind
  the streaming reply generator. The event listener specifies `cancels=[reply_event]` to
  instruct Gradio's queue runner to terminate client delivery immediately.
- **Client-side audio silencing:** Gradio's streaming `gr.Audio` component does not offer a
  native JavaScript API to flush buffered audio frames. On Stop click, frontend JavaScript
  synchronously pauses all `<audio>` elements, sets `currentTime = 0`, clears `src`, and reloads
  them (`load()`). This silences playback instantly without waiting for a server round-trip.
- **Active generation invalidation:** The frontend observer drops `window.rivaCurrentGeneration`
  and sets `rivaGenerationRecorded = true`. Any late audio chunks or delayed `play` events from
  cancelled runs are discarded and not attributed.
- **Worker termination and queue draining:** On the backend, `ReplyManager.stop()` sets
  `run.cancelled`, drains all buffered phrases and audio chunks from `run.phrases` and
  `run.audio`, and persists the run with `status="cancelled"`. Generator loops exit promptly
  without yielding stale audio.
- **Retry semantics:** Unlike completed replies (which double-click idempotency preserves),
  a cancelled run is discarded if the user presses **Antwort erzeugen** again for the same turn,
  initiating a fresh generation with a new UUID.

### Latency and playback recording
- `stopped_ms`: client elapsed time from request start to Stop click.
- `playback_duration_ms`: duration of audible playback before interruption
  (`stopped_ms - first_browser_play_event_ms`). If cancelled prior to playback, remains null.
- `playback.stopped` is logged to the structured event stream with session, turn, generation,
  and playback duration.

### Recorded native Gradio limitations
1. **No native audio stream flush/reset:** `gr.Audio(streaming=True)` continues playing any
   chunks already buffered in the browser even after server-side cancellation. Explicit DOM-level
   audio element unloading is necessary to silence output.
2. **Cooperative generator exit:** Gradio queue cancellation marks the job dead on the server
   and ceases SSE transport, but the underlying Python generator remains alive until its next
   iteration or until explicit cooperative thread termination checks the cancel flag.
3. **Play signal unreliability:** Streamed audio does not reliably fire the standard browser
   `play` event under Gradio's player wrapper across all browsers; server-side frame generation
   timing plus explicit client stop timestamps provide the only consistent bounds.

## Offline evidence and live verification

Mock tests verify speech begins before text completion, phrase/PCM order, byte
fragment reassembly, authoritative completion, failed/partial persistence, private
permissions, safe error handling, double-click idempotence and session ownership.
An integration test chains a saved synthetic ASR result through the UI handlers,
encodes/flushes actual Gradio streaming output, and records a synthetic browser
Play event. No paid API calls are made by these tests.

Live verification must check German voice quality, streamed text, audible output,
autoplay behavior, second-turn isolation, latency semantics and provider/account
access. Keep the trailing pause: unsent final chunks remain a native-Gradio
limitation (`riva-0jw.9`). No full cancellation or barge-in is claimed.

References checked:
https://developers.openai.com/api/docs/guides/streaming-responses
https://developers.openai.com/api/docs/guides/text-to-speech
https://developers.openai.com/api/docs/models/gpt-4.1-mini
