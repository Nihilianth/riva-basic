# Demo architecture

Only implemented behavior is described here; planned parts are tracked in Beads (epic `riva-skm`).

## Reused components (existing, verified)

Browser VAD capture and hidden submit, segment validation and private storage, per-session ASR, hold/merge
of unanswered transcripts, barge-in cancel, phrase-wise TTS, reply audio endpoint with Web Audio player.
See `docs/handoffs/2026-11-05-vad-pipeline.md`.

## Composition

`riva.experiments.voice_reply.build_voice_api(demo, replies)` returns the FastAPI app (Gradio page + playback,
reply-audio and diagnostics routes). `PLAYBACK_JS` is the page script. Tests build the same app on an
ephemeral port with fake providers.

## Browser player diagnostics

`PLAYBACK_JS` exposes read-only `window.rivaPlayerStats` (`counts` per stage, `chunks`, `ctx`) next to the
stage line; browser tests and operators can read how the player behaved without audio output.

## `riva.demo` package (so far)
- `text.py`: German heuristics (`looks_german`), tolerant word matching, `grounding_score`.
- `schema.py`: turn schema, `parse_turn` (structure), `validate_turn` (hard gates + soft flags).
- `config.py` + `defaults/*.toml`: personas, models, voices, limits (validated).
- `prompts.py`: `build_instructions(core + persona)` and `build_input(TurnContext)`.
Not yet built: processor with provider interfaces, journal, session lifecycle, archive, UI.

## Shared segment pipeline (`riva.experiments.asr_stream`)
The developer voice app and the demo share these pieces; behavior is covered by the existing tests:
- `SegmentTimeline`: allowlisted diagnostic stages (no content), mirrored to the session for the UI poll.
- `process_segment()`: validate the VAD payload, save the exact segment privately, run ASR; yields
  `SegmentProgress` and returns a `SegmentOutcome` (or `None` when rejected/not persisted).
- `gate_transcript()`: the hold/merge rule - while the user speaks (or spoke again after this segment) the
  transcript is held; otherwise held + new text are merged into one turn (`TurnDecision`).
- `vad_segment_updates()`: the developer-UI wrapper (reply streaming and its tuple outputs).
- `ReplyManager.start(..., config)`: a per-session `ReplyConfig` (voice, `tts_style`, models) overrides the default.

## Turn processing (`riva.demo.turn`, `providers`, `fakes`, `openai_llm`)
`TurnProcessor.process(context, persona_id, model, revisable_ids)` builds instructions (core + persona) and
the per-turn input, calls the injected `TurnLLM`, validates the result (`schema.validate_turn`) and returns a
`TurnOutcome` (`source` llm|fallback, `spoken_reply`, `journal_op`, soft flags, attempts, error codes, latency,
token usage). An invalid result is retried once with a hint naming the error code; after that, or after any
provider exception (no retry, message discarded), the outcome is the German fallback reply plus the
verbatim transcript as the journal passage (marked uncertain). `Providers` bundles the LLM, speech
synthesizer and ASR wire factory: `real_providers()` (OpenAI, paid) or `fake_providers()` (offline).
`SpokenReplyProvider` lets the existing `ReplyRun` stream an already-decided reply to TTS.

## Journal (`riva.demo.journal`)
Append-only `Passage` list (`p0`, `p1`, ...; text, turn id, source llm|verbatim_fallback, uncertain flag,
`revisions` history). `Journal.apply(outcome, turn_id)` is the only mutation:
- `append` adds the passage for the current utterance; a fallback outcome appends the verbatim transcript
  (marked uncertain). `none` changes nothing.
- `revise` replaces one of the last 5 passages **only if** the turn is not uncertain and has no `low_grounding`
  flag and the target is in the window; the old text stays in `revisions`. Otherwise nothing changes and a
  clarification is spoken (the model's reply if it already asks a question, else a fixed German sentence).
- `tail()` / `revisable_ids()` give the LLM the window; `text()` renders one paragraph per passage;
  `snapshot()` is JSON for the session archive.
