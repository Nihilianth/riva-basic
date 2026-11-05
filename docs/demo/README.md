# Demo 1: German journaling voice demo

Status: in development (Beads epic `riva-skm`). This page lists what exists today; it grows with the code.

Goal: a controlled-setting demo for German participants aged 14-35: speak, get a corrected cumulative
journal entry and a short spoken follow-up. Participant page `/` (no technical controls), presenter page
`/presenter`. Plan and phases: epic `riva-skm` and `docs/demo/decisions.md`.

## What exists now

- Test harness (see `testing.md`): network guard, `browser` marker, optional `e2e` group, synthetic German
  conversation fixtures.
- The experimental voice app it will reuse: `riva-voice-test` (see the top-level README and
  `docs/handoffs/2026-11-05-vad-pipeline.md`).

## Setup

```sh
uv sync --locked                                  # runtime + dev tools
uv sync --locked --group e2e                      # optional: browser tests
```
Private outputs (recordings, transcripts, logs) go to `../private/`, never into Git. Never read or commit `.env`.
