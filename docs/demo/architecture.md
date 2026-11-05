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
