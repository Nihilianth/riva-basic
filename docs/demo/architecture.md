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

## Session lifecycle and the commit gate (`riva.demo.session`)
`DemoSession` = one page load. State: `epoch`, `closed`, `Journal`, recent history, explained words, whether
the last reply was heard. Stages (`segment_updates`, `answer`) capture an `EpochToken` and pass the gate
under the session lock before any side effect; `close()` closes the gate first, then releases the reply run
and the ASR turn (which also drops held transcripts). Late results are not applied; they are written to the
**old** session's timeline as `stale_discarded` with the stage (`asr`, `after_asr`, `before_llm`,
`after_llm`). Blocking provider calls cannot be aborted, so their threads finish and the result is dropped.
`close()` is idempotent; a participant reload creates a new session (new identity and archive folder).
Speech control: `stop_speech()` (Stopp button) cancels the reply and marks it not heard;
`note_user_speech_started()` (barge-in) marks the last reply not heard when the user speaks while its audio
should still be playing. The next LLM input then says the reply was not fully heard, so an unheard
question may be asked again.

## Session archive (`riva.demo.archive`)
Content-bearing, private only (`../private/research-data/demo-1/<user>/<session>/`, files 0600): `session.json`
(settings snapshot, times), `timeline.jsonl` (ISO-timestamped events with relative paths), `utterances/<id>.json`
(verbatim ASR per segment, merged transcript, LLM input, raw model outputs, outcome, journal change, spoken
reply), `journal/NNNN.json` snapshots, `journal.md`; per-segment audio/transcripts stay in the turn folders
written by `RecordingStore`. Nothing in this archive is written to stdout; the ordinary event log carries
names and opaque ids only (asserted by tests).

## Participant page (`riva.demo.ui`, `riva.demo.app`, `riva.demo.texts`)
`riva.demo.app.DemoRuntime` is shared by all sessions (config, providers, `AsrSessions`, one `ReplyManager`
using `SpokenReplyProvider`, defaults); `ensure(identity)` creates a `DemoSession` per page load, `release`
closes it. `build_demo_api` mounts the participant Gradio page on the same FastAPI app as the proven
playback/reply-audio/browser-event routes (`build_voice_api`). All visible strings live in `texts.py`.
Flow of one utterance in `ui.vad_segment`: `DemoSession.segment_updates` (ASR) -> `on_segment` (hold while
the user keeps talking, else merge) -> `answer` (LLM turn, commit gate, journal, archive, TTS start) ->
iterate the `ReplyRun` so its bounded audio queue drains while the page plays the audio from
`/riva/reply_audio/<id>`. The poll (300 ms) publishes `DemoSession.view()` (phase, journal, verbatim, reply,
generation id) and the server phase; the page combines it with what only the browser knows (user speaking,
audio playing) to show one indicator (`INDICATOR_JS`). Start is a real click, which also unlocks audio.

## Session lifecycle: creating is not closing (riva.demo.app.DemoRuntime)
| Event | What happens |
|---|---|
| Page load / reload | A new Gradio page state has a **new identity**; `ensure(identity)` **creates** a new isolated session (own journal, archive folder, ASR turn). Nothing is shared with the old page. |
| Page unload (reload, tab closed) | `pagehide` sends `navigator.sendBeacon('/riva/session_end')` with the page's session id (published by the poll); the old session is **closed immediately** (reason `page_unloaded`). Best effort. |
| Presenter reset | `runtime.reset(id)` / `reset_all()` **closes immediately** (reason `presenter_reset`); the participant page sees phase `closed`, reloads itself and gets a new session. |
| Abandoned page (crash, sleep, network loss) | The page polls every 300 ms; `reap()` closes sessions idle for more than `idle_timeout` (180 s, reason `expired`). It runs periodically whenever any page is active. |
| Gradio state cleanup | `release(identity)` closes (reason `released`) as the final backstop. |
A closed session stays in `runtime.sessions` as a record (the newest 50 are kept). It rejects every late result
through the commit gate (`stale_discarded` in ITS timeline), is never recreated for the same identity, and
its archive keeps `session_closed` with the reason. Close is idempotent. Tests: `tests/demo/test_lifecycle.py`
(injected clock) and the reload/reset Playwright tests.

## Pre-flight checks (`riva.demo.preflight`)
Three kinds, shown in three groups on the presenter page:
- **automatic** (free, silent): archive writable and outside Git; `OPENAI_API_KEY` present (never shown); each
  allowed LLM model and the TTS model exist (`models.retrieve`, no cost); an ASR session can be opened (no audio
  sent); configured voices valid. Unreachable service and unavailable model are reported differently; provider
  error text is never shown.
- **interactive** (browser/person): microphone permission (`getUserMedia`), audio playback (test tone that the
  presenter confirms hearing).
- **paid** (one real TTS request, a few cents): only after an explicit confirmation (`paid_voice_test` refuses
  otherwise); it is the only way to know that a voice really speaks.
Probes sit behind `ServiceProbe` (`OpenAIProbe` real, `FakeProbe` offline); the real probe is not run in tests.
