# Demo testing

Source of truth for behavior is the code and these tests; this page explains how to run them.

## Layers

| Layer | Where | Needs | Marker |
|---|---|---|---|
| Unit / component / mocked pipeline | `tests/` (existing flat files), `tests/demo/` | nothing | none |
| Browser (Playwright, headless Chromium) | `tests/demo/browser/` | `e2e` group + Chromium | `browser` |
| Live (real ASR/LLM/TTS) | `tests/demo/live/` (planned) | `RIVA_RUN_LIVE_TESTS=1`, API key, explicit approval | `live` |

Rules enforced by `tests/conftest.py`:
- An autouse **network guard** makes any non-loopback connection or DNS lookup fail in every test that is
  not marked `live`, so a paid call cannot happen by accident. Chromium is a separate process and is not
  affected by the guard.
- `live` tests are skipped unless `RIVA_RUN_LIVE_TESTS=1`.
- Browser tests skip with a clear reason when Playwright or Chromium is missing, so plain `uv run pytest`
  never depends on a browser.

## Commands

```sh
uv run pytest -q                              # everything that is available (browser skips if missing)
uv run pytest -q -m "not browser"             # deterministic, no browser
uv sync --locked --group e2e                  # one-time: Playwright (dev-only group)
uv run --group e2e playwright install chromium   # one-time: browser into ~/.cache/ms-playwright
uv run --group e2e pytest -q -m browser       # browser tests only
```

### Chromium on WSL/Ubuntu without sudo

Chromium needs `libnss3`/`libnspr4`. If they are missing the browser tests skip with a hint. Either
`sudo apt install libnss3 libnspr4`, or run `scripts/fetch-chromium-libs.sh` (downloads the `.deb` files as a
normal user and unpacks them into `~/.cache/riva-chromium-libs`; the fixture in
`tests/demo/browser/conftest.py` adds that folder to `LD_LIBRARY_PATH`). Override the location with
`RIVA_CHROMIUM_LIBS_ROOT`.

## Fixtures

`tests/fixtures/demo/*.json` are **synthetic** German conversations (`synthetic: true`, `language: "de"`):
`turns[] -> segments[] {asr_text (what ASR produced, errors included), audio_ms}`, `intended`, and a loose
`expect` block (journal operation, uncertainty, clarification, required/forbidden words, flags).
Scenarios: Zoo example from the brief, incomplete sentences, self-correction (revise), ambiguous correction,
stuttering, ambiguous reference, ASR homophone error, silence hallucination, distress mention, vocabulary
offer. `tests/support/conversations.py` loads and validates them (`tests/demo/test_conversation_fixtures.py`).
The older `tests/fixtures/conversations/` files belong to the experiment apps.

## Embedded JavaScript

The page scripts live in Python strings (`VAD_BROWSER_JS`, `PLAYBACK_JS`).
`tests/demo/test_embedded_js.py` runs `node --check` on them (skipped when `node` is absent).

## Known limits

Headless Chromium verifies page logic, DOM, status text and (planned) the VAD pipeline against prerecorded
clips. It does not verify real microphones, speaker echo, audio quality, or browsers other than Chromium.
