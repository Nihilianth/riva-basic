# Gradio audio baseline

Status: manual capture/save/playback and live German ASR with a trailing pause are
confirmed by the user in the Windows browser test. Immediate stop exposed a duplicate
tail in both audio and text; its mitigation awaits browser retest. TTS, continuous
listening and interruption remain unimplemented. Execution tasks live in Beads.

## Run the initial capture test

Use `uv run riva-audio-test` in WSL and open `http://localhost:7860` in Windows
Chrome/Edge. Record, stop, and explicitly save. Replay the saved recording.
No ASR, LLM, TTS, live chunks or automatic endpointing is wired up in this step.

The default limit is 120 seconds. The upload limit is 30 MB. Audio is preserved at
the decoded input sample rate/channel count and written as PCM16 WAV; float/int32
inputs are converted to PCM16 without peak normalization. Metadata records duration,
frames, channel count, checksum, opaque IDs and **save time**, not speech timestamps.
It explicitly marks transcription as not requested. No empty transcript or fake SRT
is generated. The next task adds streaming ASR.

Storage is per development user/session/turn, outside Git. A browser session gets
a new opaque session ID; the development user is `local-development`, not an
authenticated participant. Refreshing creates a new session. Saving twice creates
two turns. Do not use this unauthenticated test app with participant data.

Gradio may keep temporary recording/playback copies; its cache has periodic cleanup
configured (five-minute sweep, one-hour age threshold). This is not a deletion
guarantee across crashes/restarts and does not affect persisted recordings. Review
the private cache manually under an explicit retention policy before real tests.

### Manual browser verification still required

Record OS/browser versions and verify permission prompt, short German recording,
saved playback, displayed duration/sample rate, a second recording, refresh, and
clearing the widget without accidentally deleting saved data. Also test empty input
and over-limit recording. Report any conversion error and verify `ffmpeg -version`
inside WSL. A headless HTTP check is not evidence of microphone functionality.

## Goal

Determine whether Gradio's native audio input/output is sufficient for the
ASR → text LLM → TTS chain, manual completion, continuous listening, and interruption.
Start on Windows Chrome/Edge against WSL localhost; repeat on the test server.

## Acceptance and evidence

- Capture a valid user-turn recording, duration, sample rate, and identifiers.
- Feed ordered/resampled chunks into one persistent OpenAI transcription session,
  display provisional/final text, and save audio + TXT + canonical JSON.
- Start spoken output before the full response's audio has been produced.
- Explicit stop clears playback, pending audio, and stale generation output.
- Test speech-triggered interruption with headphones first, then speakers.
- Manual mode never commits a turn solely because of silence; continuous mode has
  configurable endpointing and a visible completion/stop control.
- Record ASR-final latency, first-text latency, first-audible latency, and
  interruption-to-silence. Browser playback measurements are required, not just
  server-side response timestamps.
- Document timestamp support. SRT is optional until reliable timing exists;
  approximate timings must be explicitly labelled.

## Synthetic scenarios

German long pause, silence-only input, revised partial transcript, user interruption
with late response chunks, provider failure, and browser disconnect. Never claim
the fixture validation tests already implement these future audio behaviors.

## Result template

Record date, commit, browser/OS, WSL networking mode, model IDs, audio format,
chunk interval, endpointing settings, sample counts, latency distribution, playback
behavior, failures, and conclusion: keep native audio or investigate another transport.

## Initial backend evidence — 2026-10-21

Python 3.12.3, Gradio 6.30.0, Linux FFmpeg 6.1.1. The localhost process serves its
configuration over HTTP 200 with two audio components and analytics disabled.
Thirty offline tests pass, including PCM16 round-trip, stereo/float/int32 input,
duration/type rejection, atomic-write failure cleanup, private permissions and
Gradio callback/routing checks. Ruff, mypy, pre-commit/Gitleaks and package build pass.
These results do not establish microphone, playback, streaming, or latency behavior
in Windows Chrome/Edge. No transport decision has been made beyond testing native
Gradio first.

## First browser finding and playback fix — 2026-10-21

The user could record in Windows despite an initial microphone-discovery message.
Saving persisted the WAV, but playback postprocessing rejected the original private
archive path with `InvalidPathError`. Earlier callback-only tests did not exercise
Gradio's launched file-access check.

The callback now explicitly copies the selected recording into the playback
component's managed cache and returns only that copy. No archive directory is added
to `allowed_paths`, and permissions on originals are unchanged. Cache-copy failures
report that the original was saved rather than implying recording persistence failed.

Regression tests enable launched-equivalent path checking with an archive outside
the simulated system temp directory. They verify that the original path is rejected,
the cache copy completes output postprocessing and is served byte-for-byte over
HTTP, and direct requests for archived audio/metadata return 403. A separate failure
test verifies that the original survives cache-write errors.

The user subsequently confirmed that saving and replaying recorded audio works.
The initial microphone-discovery warning is not diagnosed; this confirmation does
not establish every browser/version, automatic endpointing, or streaming behavior.

## Streaming ASR implementation

`uv run --env-file .env riva-asr-test` runs a separate native-Gradio streaming UI.
One backend worker owns each turn's OpenAI WebSocket and serializes audio appends
and explicit commit. Audio conversion is mono, no peak normalization, stateful
libsoxr resampling to 24 kHz PCM16. Input queues are bounded; overflow/invalid chunks
fail the transcript rather than silently producing a truncated successful result.

Partial text and authoritative completion are separate. Item IDs prevent an
unrelated completion event from finalizing a turn. Final timeout, provider failure
and disconnect/idle handling use fixed error codes without logging raw errors or
transcript content. The UI polls snapshots independently of the audio queue.

The Finish action saves received audio first, then TXT/final JSON on success or
failed JSON with any partial text on failure. Archives remain private; only playback
copies enter the managed cache. Model-provided timestamps are unavailable and no
SRT is fabricated. `saved_at` means filesystem save time, not speech timing.

Important browser transport finding: the upstream Gradio streaming recorder can
skip a final chunk still being encoded/uploaded when stop is pressed. The UI
discloses this and suggests a short trailing pause. Saved audio is explicitly scoped
to received chunks. Reliable end-of-turn integrity still needs browser measurement;
do not treat this prototype as lossless microphone capture.

Provider calls are mocked in automated tests. The user confirmed live operation
with a trailing pause; precise caption latency, chunk ordering, immediate-stop
integrity and repeated-recording behavior still need measurement. No paid API
call was used to validate this implementation automatically.

References checked: OpenAI realtime transcription and WebSocket guides:
https://developers.openai.com/api/docs/guides/realtime-transcription
https://developers.openai.com/api/docs/guides/voice-websockets?api=realtime

## Stop-signal duplication — delivery-identity mitigation

The user reported that stopping immediately after speech duplicated the last chunk
in both saved replay and final text. This is audio-path corruption, not merely an
LLM/transcript formatting issue. Existing recordings are left unchanged.

In installed Gradio 6.30.0, the stream-close route wakes the queue without replacing
its last `PredictBodyInternal`. `Queue.wait_for_batch` can return that closed stream
as awake, and the processing loop can invoke the callback with the unchanged body.
The old append callback treated every invocation as new audio.

A small ASGI middleware now stamps audio HTTP requests at `/queue/join` and
`/stream/{id}` before Gradio queues them. A close-triggered callback keeps the same
stamp and is skipped. A genuinely new delivery gets a new stamp even if the waveform
is identical. The guard therefore preserves intentional repetition and silence;
it does not compare words, hashes or sample overlap. Stamps are per incoming request,
not capture timestamps, sequence numbers, authentication tokens or network-retry IDs.

Regression coverage includes the actual Gradio queue-wakeup helper, actual HTTP
queue/update/close routes with mocked queue admission, real EventData argument
injection, and session persistence. It verifies two identical new deliveries are
both kept while reprocessing either delivery does not append it again. Middleware
is supplied via launch `app_kwargs` because Gradio recreates its FastAPI app at
launch. New callbacks fail closed if a delivery stamp is missing.

Skipped replays appear as `stream.replay_skipped` events and a count in UI status.
The unsent-tail issue is separate: this change does not guarantee complete capture
at an abrupt stop. A no-pause browser retest is still required before claiming the
observed duplication is resolved on the user's device. Beads: `riva-0jw.10` (fix),
`riva-0jw.9` (broader transport integrity investigation).
