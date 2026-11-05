# Demo testing

Source of truth for behavior is the code and these tests; this page explains how to run them.

## Reading test results

`passed`, `failed`, `skipped` and `deselected` mean different things:
- **skipped** = a test was collected but a prerequisite is missing; `uv run pytest -rs` prints the reason
  (no Playwright/Chromium, no cached VAD assets -> `scripts/fetch_browser_test_assets.py`, no `node`,
  `live` tests without `RIVA_RUN_LIVE_TESTS=1`).
- **deselected** = excluded on purpose with `-m` (for example `-m "not browser"` deselects the browser tests).
- **failed** is always a real problem. Expect 0 passed-with-skips to be explained by the list above.

Last verified on this machine (Chromium, assets and node installed): all collected tests passed, 0 skipped,
0 failed; `-m browser` selects the 5 browser tests and deselects the rest; with the VAD asset cache missing
4 browser tests skip with the hint above.

## Layers

| Layer | Where | Needs | Marker |
|---|---|---|---|
| Unit / component / mocked pipeline | `tests/` (existing flat files), `tests/demo/` | nothing | none |
| Browser (Playwright, headless Chromium) | `tests/demo/browser/` | `e2e` group + Chromium | `browser` |
| Live (real ASR/LLM/TTS) | `tests/demo/live/` | `RIVA_RUN_LIVE_TESTS=1`, API key, explicit approval | `live` |

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

## Live LLM smoke test (paid, opt-in)

`tests/demo/live/test_llm_smoke.py`: 7 calls on synthetic German cases (self-correction, ambiguous
correction, ASR homophone, stutter, follow-up). Run only with approval:
`RIVA_RUN_LIVE_TESTS=1 uv run --env-file .env pytest -m live tests/demo/live -s`. Bounded by construction
(max 14 calls, 700 output tokens each, abort at about $0.25 estimated; prices in the file are approximate).
It asserts schema compliance; meaning is judged from the saved results under
`../private/research-data/demo-1/live-eval/`. Without `RIVA_RUN_LIVE_TESTS=1` it is skipped.

## Fake microphone (real VAD against prerecorded speech)

`tests/demo/browser/test_fake_microphone.py` starts the real app (`running_voice_app` in
`tests/support/server.py`: ephemeral port, deterministic ASR/LLM/TTS fakes, everything else real) and drives
it with headless Chromium whose microphone plays a WAV
(`--use-fake-device-for-media-stream --use-file-for-fake-audio-capture=<wav>%noloop`). The real MicVAD,
hidden submit button, Gradio queue, hold/merge gate, reply-audio endpoint and Web Audio player run.
Scenarios: speech -> ASR -> reply scheduled; background noise ignored; a fragmented utterance (pause longer
than the VAD threshold) is held and sent to the "LLM" once as the merged text; speaking over a reply
suspends and drops it and a new reply follows.

- **Clips**: `tests/fixtures/speech/{zoo,elefanten,schwimmbad}.wav` are synthetic (OpenAI TTS, 16 kHz mono,
  `manifest.json` lists text/voice/model/hash). `scripts/generate_speech_fixtures.py` makes them; it is
  **paid**, run only with approval, and refuses to overwrite without `--force`. `tests/support/audio.py`
  composes silence/noise around them per test.
- **VAD assets** (vad-web, onnxruntime-web, Silero model; ~45 MB) are served from a local cache via Playwright
  routing: run `uv run python scripts/fetch_browser_test_assets.py` once (free; `RIVA_BROWSER_ASSETS`
  overrides `~/.cache/riva-browser-test-assets`). Tests skip with a hint when the cache is missing.
- **Slow ASR** is simulated with `tests/support/fakes.py::delayed_wire` so a second utterance can start
  before the first transcript is final, as in real use.
- **Player assertions** use `window.rivaPlayerStats` (stage counters, scheduled chunks, context state), since
  headless Chromium has no audible output.

## Participant page tests (`tests/demo/browser/test_participant_ui.py`)

Real demo page, real VAD with the fake microphone, deterministic slow providers so each phase is visible:
German-only visible text and `lang="de"`; no technical control on the page (only Start, Stopp, tabs and the
closed spoiler; no checkbox/slider/file inputs; no Gradio footer); every status phase appears in order
(idle, starting, listening, hearing, transcribing, thinking, preparing, speaking, listening); the journal and
"heard" tabs fill; Stopp ends speech at once; a presenter reset closes the session and the page reloads into
a new one; a failing ASR shows the friendly error and recovers; a failing LLM keeps the participant's words
(verbatim fallback, no provider text shown); keyboard Tab order reaches Start then Stopp with a visible
focus outline and Enter starts; **axe-core** (WCAG 2 A/AA, cached by `fetch_browser_test_assets.py`) reports
no serious or critical violation. Contrast fixes found by axe are in `riva.demo.ui.CSS`.

## Conversation tests (`tests/demo/browser/test_demo_conversation.py`)

Whole conversations in the demo app with the fake microphone: two utterances build a two-paragraph journal
and the second prompt carries the first passage and the earlier exchange; a fragmented utterance (pause
longer than the VAD threshold, slow ASR) is held and becomes ONE LLM call and ONE journal entry while the
"heard" tab shows both fragments; speaking over a reply suspends it and the next prompt says it was "nicht
ganz gehört"; background noise never reaches the journal; the private archive on disk holds the timeline,
the verbatim ASR, the journal and the audio/transcript files, while the ordinary log (stdout) contains no
transcript text. Shared helpers: `tests/support/{audio,fakes,server,ui,demo,scripted,conversations}.py`.

## Embedded JavaScript

The page scripts live in Python strings (`VAD_BROWSER_JS`, `PLAYBACK_JS`).
`tests/demo/test_embedded_js.py` runs `node --check` on them (skipped when `node` is absent).

## Known limits

Headless Chromium with prerecorded synthetic clips verifies page logic, DOM, status text and the VAD ->
backend -> player chain. It does **not** verify real microphones or their processing (echo cancellation,
gain), speaker echo, audible quality or gaps, real ASR accuracy on atypical speech, or browsers other than
Chromium. Each browser scenario takes ~10 s; the four fake-microphone tests together ~45 s.
