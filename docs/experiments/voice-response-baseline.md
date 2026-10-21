# Manual voice response baseline

Date: 2026-10-22. Beads: `riva-0jw.3`.
Status: implemented and tested offline; live LLM/TTS and browser playback verification pending.

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
wall-clock/server/client clocks. Generation-ID validation rejects stale Play reports.
An absent Play event remains null; replay/resume does not overwrite the first event.
The Play event is only a proxy: a muted device, stalled buffer or hardware latency
means it is not proof of first audible sound. Measure audible latency separately.

## Model lifecycle

`gpt-4.1-mini` is the low-latency, non-reasoning text baseline. Dedicated speech uses
`gpt-4o-mini-tts` with `coral`. Current OpenAI docs mark Speech API models deprecated,
with shutdown January 6, 2027. `riva-ddc` tracks migration to a supported workflow
while preserving the separate text orchestration/TTS boundary and checking fidelity.
The recommended Realtime model cannot simply be supplied to `/v1/audio/speech`.

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
