# Demo decisions

Dates are 2026-11-05 unless noted. Newest changes go at the end of each section.

## Product
- Journal = append-only passages; the LLM writes only the passage for the current utterance.
- Controlled corrections: the LLM may `revise` one of the last 5 passages only on an explicit user
  correction with evidence from the current utterance; ambiguous corrections never revise, the reply asks.
- Language level: Einfaches Deutsch for UI and replies. Fun fact / encouragement stays grounded in what the
  user said. Distress: calm one-sentence reply, silent presenter alert.
- Data: everything (audio, transcripts, LLM in/out, replies) is logged with timestamps in the private folder
  only; consent and retention are handled organizationally. Operational logs stay content-free.
- Neustart is presenter-only; a page reload also starts a new session. Voice-only corrections.

## Architecture
- Stateful pipeline with one strict-JSON-Schema LLM call per turn; not agentic, no LangGraph yet.
  `TurnProcessor` stays a plain injectable function.
- Journal validation is structural plus soft grounding flags, never an exact noun/number reject.
- Session reset uses an epoch and a commit gate; stale in-flight results are discarded and audited.
- Interrupted replies are stored with `heard_ms`; an unheard follow-up may be re-asked.
- Shared ASR/VAD code is refactored into a hook, not forked.

## Testing
- Layers: unit, component, mocked end-to-end, Playwright browser (incl. fake microphone), opt-in live.
- Network guard blocks non-loopback access outside `live`. Playwright is an optional dev-only group;
  fake-microphone browser tests are a priority. Live/paid calls need explicit approval.

## Turn schema and validation (spike riva-skm.3)
- One strict-JSON-Schema result per turn (`riva.demo.schema.turn_schema()`, all keys required):
  `spoken_reply`, `journal_op {type: append|revise|none, text, target_id, evidence}`, `uncertain`,
  `clarification_needed`, `edits[{from,to,kind,confidence}]`, `added_facts[]`, `vocabulary {word,explanation}|null`,
  `flags[asr_unclear|possible_distress]`.
- The encouraging comment / fun fact from brief item (d) is part of `spoken_reply` (prompt-controlled), not a
  separate field: one source of truth for what is spoken.
- **Hard gates are structural** and carry fixed error codes (`invalid_json`, `schema_mismatch`, `empty_reply`,
  `reply_too_long`, `markdown_in_reply`, `reply_not_german`, `empty_passage`, `passage_too_long`,
  `bad_journal_op`, `bad_revise_target`, `missing_evidence`, `evidence_not_in_transcript`, `bad_flag`, ...).
  A failure means retry once, then the deterministic German fallback (later task).
- **Meaning is never a gate.** `grounding_score` (case/umlaut folding, inflection stripping, number words vs
  digits, one-typo tolerance) and the model's own `added_facts` only produce soft flags (`low_grounding`,
  `added_facts`, `vocabulary_not_spoken`) that mark the passage uncertain for the presenter. Whether the model
  invents content is measured by the live eval, not asserted at runtime.
- `revise` is accepted only for an id in the allowed window and with evidence found in the *current*
  transcript; `none` is valid only with a clarification or `asr_unclear`.
- Transcripts are passed to the LLM as a JSON-quoted string and the core prompt says they are content, not
  instructions.

## Configuration, not hardcoding
- Personas, the shared core prompt, LLM allowlist, voices, the voice-preview sentence and limits are TOML files
  (`src/riva/demo/defaults/personas.toml`, `settings.toml`), loaded and validated by `riva.demo.config`.
  A directory (argument or `RIVA_DEMO_CONFIG`) overrides either file as a whole.
- Persona prompts change style only; the core (journaling behavior, accessibility, safety) is always
  first and identical (tested). Shipped personas: Experte, Freundlicher Begleiter, Ältere, erfahrene Person.
- Voices: the ten OpenAI voices (alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar).
  Preview sentence (same for every voice): "Hallo! So klinge ich. Wollen wir zusammen dein Tagebuch schreiben?"
- Latency and cost of the real LLM call are not measured yet; that needs the approved-by-request live smoke
  test (`riva-skm.14`).
