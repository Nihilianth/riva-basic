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
