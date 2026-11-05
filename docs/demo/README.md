# Demo 1: German journaling voice demo

Status: in development (Beads epic `riva-skm`). This page lists what exists today; it grows with the code.
Experimental, for a controlled setting; not a participant-ready product.

Goal: a controlled-setting demo for German participants aged 14-35: speak, get a corrected cumulative
journal entry and a short spoken follow-up. Participant page `/` (no technical controls), presenter page
`/presenter`. Plan and phases: epic `riva-skm` and `docs/demo/decisions.md`.

## Run it

```sh
uv run riva-demo --fake                        # offline: fake ASR, LLM and TTS, no paid calls
uv run --env-file .env riva-demo               # real OpenAI ASR, LLM and TTS (paid)
```
Open http://localhost:7863 in Chrome, press **▶ Start**, allow the microphone and talk. Options:
`--port`, `--private-dir` (default `../private`; recordings, transcripts and the session archive go to
`<private>/research-data/demo-1/`), `--config-dir` (folder with `personas.toml` / `settings.toml` overrides).
Each page load is its own session. The first request to the real services is billed by OpenAI; use `--fake`
to practise.

Voice previews are cached under `<private>/research-data/demo-1-cache/voice-previews/` (key: model + voice +
sentence). A voice's first preview costs one TTS request (a fraction of a cent); the presenter page has
**Alle Stimmen vorbereiten** (needs the cost tick) to prepare all ten before the demo.

Customize without code: personas, the shared journaling/safety rules, the LLM allowlist, voices, the
preview sentence and limits are in `src/riva/demo/defaults/*.toml` (copy them into a `--config-dir`).

## What exists now

- **Participant page** (`/`): a short friendly introduction ("So geht's", plain text, nothing plays or listens
  before Start), German, large Start and Stopp buttons, one status indicator
  (😴 Drücke auf Start · 👂 hört zu · 🗣️ hört dich · ✍️ schreibt mit · 🤔 denkt nach · ⏳ bereitet vor ·
  🔊 spricht · ⚠️ Fehler), tabs "Mein Tagebuch" (journal, front) and "Das habe ich gehört" (verbatim ASR),
  the last spoken reply, a **Stimme** dropdown (picking a voice plays one short preview sentence, the same for
  every voice, and the voice is used for the next reply), and a closed spoiler with read-only technical details.
- **Presenter page** (`/presenter/`, German; same machine): (1) pre-flight checks in three groups - runs
  automatically / needs you (microphone permission, test tone) / costs money (voice test, only after a
  confirmation tick); (2) settings - persona, language model, voice, pause threshold - saved for NEW sessions or
  applied to the selected running session; (3) sessions - live table, **Gewählte Sitzung beenden (Neustart)**
  (closes at once, the participant page reloads into a new session), **Alle beenden**, **⏹ Alles anhalten**
  (stops speech in every session, also the audio already buffered in the browser), journal, verbatim text,
  timeline and silent alerts (possible distress) of the selected session. The participant page contains none
  of this. The pause threshold (500-3000 ms, default 900) is read when a participant presses Start.
- Journaling turn: ASR, hold/merge of fragments, structured LLM turn, journal (append, controlled
  revision), spoken reply, private session archive.

- Test harness (see `testing.md`): network guard, `browser` marker, optional `e2e` group, synthetic German
  conversation fixtures, and fake-microphone browser tests that run the real VAD against synthetic speech.
- The experimental voice app it will reuse: `riva-voice-test` (see the top-level README and
  `docs/handoffs/2026-11-05-vad-pipeline.md`).

## Setup

```sh
uv sync --locked                                  # runtime + dev tools
uv sync --locked --group e2e                      # optional: browser tests
uv run --group e2e playwright install chromium    # once; see testing.md for libnss on WSL
uv run python scripts/fetch_browser_test_assets.py  # once: cached VAD files (free)
```
Private outputs (recordings, transcripts, logs) go to `../private/`, never into Git. Never read or commit `.env`.

## What the Stopp button does (verified by tests)

| Moment | Effect of **⏹ Stopp** |
|---|---|
| A reply is audible, or still being synthesized | The audio stops at once in the browser; the server cancels the reply and marks it "not fully heard", so the next turn may ask an unheard follow-up again. The journal entry is kept. The page returns to "Ich höre dir zu." |
| The participant is speaking / being recorded | **Nothing is stopped.** Recording and listening continue, the utterance is processed normally and its reply is not affected. |
| Transcribing or thinking (no reply exists yet) | Nothing to stop: Stopp does not cancel speech recognition or the language model, so the reply that is about to start will play. Press Stopp again when it starts. (Known limitation.) |
| Always | Stopp never switches the microphone off and there is no "pause listening" button; the presenter can end the session. |
Starting to speak while a reply plays also stops it (barge-in), independent of Stopp. **Alles anhalten** on the
presenter page does the same as Stopp for every active session.

## Keep the demo local-only

Neither page has a login. The server listens on `127.0.0.1` only and has **no option to change that**; in
addition `/presenter` answers loopback clients only and returns 403 otherwise (tested). Do not expose the demo
with port forwarding, a tunnel, a VM bridge or a reverse proxy: behind a proxy on the same machine remote users
appear as loopback and the guard no longer helps. The presenter page shows journals, transcripts and timelines and
can reset sessions and start paid requests; it must stay local unless real access control is added first.

## Deleting a session and its recordings

Everything of one session - segment recordings, transcripts, reply audio, LLM prompts and outputs, journal and
timeline - lives in ONE folder: `../private/research-data/demo-1/<user>/<session-id>/`.
1. Find the id: the first 8 characters are in the presenter's session table; the full id is in `session.json`
   (`ls -t ../private/research-data/demo-1/local-development | head`).
2. Close the session (presenter: **Gewählte Sitzung beenden**) or stop the demo. The tool refuses open sessions.
3. Look first, then delete: `uv run python scripts/delete_session_archive.py <id>` (dry run: lists files,
   recordings and size), then `uv run python scripts/delete_session_archive.py <id> --delete`.
   Only that session's folder is removed; ambiguous ids, symlinks and anything outside the archive are refused.
4. Check: `ls ../private/research-data/demo-1/local-development` no longer shows the id.
To remove all demo data: stop the demo, then delete the folder `../private/research-data/demo-1/` yourself.
What the tool does **not** cover: your own backups or copies, data kept by the providers under their own policies
(requests use `store=False`; audio/transcription terms are OpenAI's), the voice-preview cache
(`demo-1-cache`, not participant data) and any copy in `${GRADIO_TEMP_DIR:-/tmp/gradio}` (the demo itself does
not put audio there). Ordinary logs and the terminal contain ids only, never content.

## Before the demo (checklist)

Automated tests cannot cover these; do them once on the real laptop, headset/speakers and network:
1. `uv run --env-file .env riva-demo`, open `/presenter/`, press **Automatisch prüfen**: everything automatic is
   green (key, models, speech recognition, archive folder).
2. **Mikrofon prüfen** and **Testton abspielen** in the browser you will use (Chrome recommended); confirm you hear it.
3. Prepare the voices once (**Alle Stimmen vorbereiten**, a few cents) and run **Stimmtest** for the chosen voice.
4. Do one full conversation with the real microphone and the real speakers: check the hearing/thinking/speaking
   indicator, the journal text, Stopp, and speaking over a reply. Use a headset if the speakers feed back into the
   microphone (echo is untested).
5. Run the paid end-to-end smoke test (`docs/demo/testing.md`) if you want measured latency.
6. Consent for recording, and retention/deletion of `../private/research-data/demo-1/`, are your responsibility.

## Known limits

Experimental, not participant-ready. Not verified: real microphones and their processing, speaker echo, audible
audio quality and gaps, atypical-speech ASR accuracy, browsers other than Chromium/Chrome, venue network. End-to-end
latency with the real services is unmeasured until the opt-in smoke test is approved and run. The presenter page has
no login (it listens on 127.0.0.1 only). Reloading a page ends its session; the journal is not carried over.
