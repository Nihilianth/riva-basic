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

Customize without code: personas, the shared journaling/safety rules, the LLM allowlist, voices, the
preview sentence and limits are in `src/riva/demo/defaults/*.toml` (copy them into a `--config-dir`).

## What exists now

- **Participant page** (`/`): a short friendly introduction ("So geht's", plain text, nothing plays or listens
  before Start), German, large Start and Stopp buttons, one status indicator
  (😴 Drücke auf Start · 👂 hört zu · 🗣️ hört dich · ✍️ schreibt mit · 🤔 denkt nach · ⏳ bereitet vor ·
  🔊 spricht · ⚠️ Fehler), tabs "Mein Tagebuch" (journal, front) and "Das habe ich gehört" (verbatim ASR),
  the last spoken reply, and a closed spoiler with read-only technical details. Presenter page: planned.
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
