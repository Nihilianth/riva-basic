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

## Phase 0 live LLM smoke (2026-11-05, approved, bounded)
7 calls (5 cases on gpt-4.1-mini, 2 on gpt-4.1), synthetic German only, estimated cost about $0.01 (cap was
EUR 1; the test is bounded by construction: max 14 calls, 700 output tokens each, abort at $0.25 estimated).
Results are in the private archive (`research-data/demo-1/live-eval/`), not in Git.
- Schema compliance: 7/7 valid on the first attempt with strict structured output.
- LLM-only latency: gpt-4.1-mini 1.6-4.0 s, gpt-4.1 about 1.1-1.2 s; ~1,260 input tokens (mostly the core
  prompt) and 80-160 output tokens per turn, roughly $0.0015 per turn on mini. End-to-end latency is not measured.
- Meaning: good for the Zoo follow-up, explicit correction (revise), stutter removal and the ASR homophone
  ("Möwe mit Mähne": kept as spoken, marked uncertain, asked "meinst du ... eine andere Vogelart?" - no silent guess).
- **Failure found**: for the ambiguous correction ("Nein, es waren vier.") gpt-4.1-mini revised an earlier
  entry and invented "vier Elefanten und Löwen"; gpt-4.1 correctly returned `none` and asked. The validator
  flagged `uncertain` + `low_grounding` but did not stop it. Decision: **a `revise` is applied only when the
  turn is not uncertain and has no `low_grounding` flag**; otherwise the journal stays unchanged and the reply
  becomes a clarification (the model's reply if it already contains a question, else a fixed German one).
  Implemented with the journal (riva-skm.6) and covered by tests; the prompt also gets a worked example.

## Revisions stay conservative (2026-11-05, requirement from the user)
- An ambiguous or doubtful correction never edits a passage. The original passage text is never lost: every
  revision stores the previous text, the turn, **the utterance id and the participant's utterance** that
  caused it (`Revision`), also in the archive's journal snapshots. A blocked correction is logged as
  `revise_blocked` and the triggering utterance stays in `utterances/<id>.json` with the reason.
- `low_grounding` (word overlap between a proposed text and what was said) is a **warning heuristic**, not a
  semantic correctness check: it can be wrong in both directions. It is only used to make the journal more
  cautious (block a revision, mark a passage uncertain), never to accept something as correct.

## Phase 2 decisions (2026-11-05)
- **Session lifecycle** (see architecture.md): creating and closing are separate. A reload is a new isolated
  session; the old one is closed by an unload beacon (best effort), by the presenter's explicit reset (immediate),
  by idle expiry (180 s without polling) or finally by Gradio's state cleanup. Closed sessions stay as records,
  reject stale results and are never recreated.
- **Presenter page** on the same server at `/presenter/` (German, same machine). Settings apply to NEW sessions;
  "apply to the selected session" changes the next LLM call and voice only (core rules never change). The pause
  threshold (500-3000 ms) is served at `/riva/settings` and read when a participant presses Start.
- **Alles anhalten** bumps a per-session counter that the participant page watches, so audio already buffered in
  the browser stops too (a server-side cancel alone would not).
- **Pre-flight**: three kinds - automatic (free probes), interactive (microphone permission, test tone) and paid
  (one real TTS request after a confirmation tick). Voice availability can only really be proven by the paid test.
- **Voice previews**: one fixed German sentence, cached per model+voice+sentence; the first preview of a voice is
  one TTS request. Previews are never generated at start-up; the presenter can prepare all after a cost tick.
- **Light theme only**: the colors and contrast fixes are designed for the light theme; a dark system setting is
  redirected to `?__theme=light` on both pages.
- **Participant introduction**: plain text "So geht's" with four steps and "Ich bin ein Computer-Programm, kein
  Mensch." No audio or microphone before Start (tested).
- **Revisions** stay conservative (see above); the low-grounding flag is a warning heuristic only.
- **Not done on purpose**: the extended live LLM eval and the real end-to-end smoke test need the user's approval
  of the paid calls (`riva-skm.14`, `riva-skm.19`).

## Real end-to-end smoke test (2026-11-05, user-approved, synthetic audio, budget EUR 1)
Two-turn conversation on the real services (realtime ASR `gpt-live-transcribe`, `gpt-4.1-mini` structured turn,
`gpt-4o-mini-tts` voice coral, persona Experte): turn 1 "Heute war ich im Zoo." (1.8 s), turn 2 "Elefanten und
einen ganz großen Löwen." (3.7 s) as the answer to the follow-up question. Run once; details in the private
archive (`research-data/demo-1/live-eval/e2e-smoke-*.json`). Browser capture and playback are NOT included.

| Stage | Turn 1 | Turn 2 |
|---|---|---|
| ASR stage total (submit -> segment saved + final) | 4.8 s | 4.8 s |
|  - connect + send audio (`asr.audio_sent`) | 1.7 s | 1.0 s |
|  - final transcript available (`asr.completed`) | 2.9 s | 2.9 s |
|  - waiting for the WebSocket to close | ~1.9 s | ~1.9 s |
| LLM (one call, 1 attempt) | 4.2 s | 1.8 s |
| TTS first audio (from reply start) | 1.9 s | 1.2 s |
| **End of speech -> first reply audio ready** | **10.9 s** | **7.9 s** |
| Reply audio produced | 5.0 s | 7.4 s |

Browser adds on top: the VAD pause threshold (0.9 s default) before the segment is submitted, plus network and
audio start; perceived delay is therefore roughly 9-12 s.
- Quality: both transcripts exact; passages "Heute war ich im Zoo." and "Ich habe Elefanten und einen ganz großen
  Löwen gesehen." (faithful, nothing added); both replies were short, German, relevant and ended with a question
  (turn 2 referred to the elephants and the lion from the answer).
- Failures/retries: none (1 LLM attempt per turn, no error codes, ASR and TTS completed).
- Cost: 2 ASR sessions (5.5 s audio), 2 LLM calls (2,597 in / 216 out tokens), 4 TTS phrases (12.4 s audio) =
  about **$0.006** by our approximate rates (actual billing was not inspected). The guard allows at most about
  $0.07 in the worst case (3 ASR sessions, 6 LLM calls, 12 TTS phrases, 90 s speech).
- **Finding (latency):** about 1.9 s of every ASR stage is waiting for the WebSocket close handshake: `LiveTurn`
  sets `done` only after `wire.close()` although the transcript is final. Another 1.0-1.7 s is spent connecting
  before audio is sent. Ticket created, not changed without the user's review.

## Final validation decisions (2026-11-05)
- Stopp acts on a reply that exists (synthesized or playing); it never stops recording or the microphone and does not
  cancel a reply that is still being thought (documented limitation; tested).
- The presenter stays local-only; enforced by the loopback bind plus a 403 guard; real access control is future work.
- Session deletion is a documented procedure with a dry-run-first script for one closed session at a time.
- The accessibility exception is exactly one Gradio upstream element (`.empty`); everything else must be clean.
- The demo is NOT declared presentation-ready: real microphone, speakers, echo and a manual usability pass with
  real hardware are the user's check (see the README checklist).

## Stopp is one consistent interrupt (2026-11-05, user request)
Supersedes the earlier "Stopp never stops recording / does not cancel ASR or LLM" limitation. Stopp now ends the
current interaction in every state through the existing epoch/commit gate (no parallel cancellation mechanism):
recording is discarded without submitting, ASR/LLM results become stale, TTS and playback are cancelled and serve
no late audio, committed journal entries and the audit trail are kept, and the page says so in German
("Gestoppt"). Start resumes. A processing acknowledgement (animated bar, and a note after 4 s) makes long waits
understandable. Known TTS/ASR retirement dates stay in `docs/handoffs` and `riva-ddc` (Speech API shutdown
2027-01-06).

## ASR latency: Proposal A implemented and measured (2026-11-05, riva-42l)
`LiveTurn` now has two separate lifecycle states: `done` (the transcript is final or failed; callers continue) and
`connection_closed` with `connection_state` (connecting, open, closing, closed, close_failed). The worker sets `done`
first, then closes the socket (`_release_connection`: never raises, always ends in a final state, bounded by the
2 s `close_timeout`). `LiveTurn.close()` waits (3 s bound) only while the transcript is still being produced;
`wait_closed(timeout)` waits for the socket; `AsrSessions.close_all(timeout)` cancels every turn and waits on a shared
deadline, returning how many sockets were still closing; `DemoRuntime.shutdown()` (registered as the server's shutdown
handler) closes sessions and then calls it. Cancellation, errors, Stopp and session isolation behave as before; a
Stopp or the next utterance never waits for a closing socket (tested). Proposal B (pre-connect) stays deferred.

Real two-turn smoke test (same synthetic clips and procedure as the baseline; est. cost $0.0079 and $0.0072):

| Metric | Baseline (before) | After, run 1 | After, run 2 |
|---|---|---|---|
| ASR stage, turn 1 / turn 2 | 4.8 s / 4.8 s | 2.6 s / 2.3 s | 2.3 s / 2.5 s |
| gap between "transcript final" and stage end | 1.9 s / 1.9 s | 0 s / 0 s | 0 s / 0 s |
| LLM, turn 1 / turn 2 (provider variance) | 4.2 s / 1.8 s | 3.9 s / 1.2 s | 2.8 s / 1.4 s |
| TTS time to first audio | 1.9 s / 1.2 s | 1.4 s / 1.2 s | 1.4 s / 1.1 s |
| **End of speech -> first audio ready** | **10.9 s / 7.9 s** | **7.9 s / 4.7 s** | **6.5 s / 5.0 s** |
Transcripts identical, one LLM attempt per turn, no errors. Saving of about 2.2-2.5 s per turn from A itself; the rest
of the difference is LLM/TTS variance. Browser VAD pause and playback start are not included.
