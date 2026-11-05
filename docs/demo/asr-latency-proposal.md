# ASR latency: proposal for review (riva-42l)

Status: **Proposal A approved, implemented and measured (2026-11-05); Proposal B deferred.** Results are in
`decisions.md`. It touches the shared ASR code, so it affects the developer apps (manual app 7860, VAD app 7861) as
well as the demo. The original proposal text follows.

## Evidence (real services, 2026-11-05, see `decisions.md`)
Per turn the ASR stage took 4.8 s, of which:

| Part | Turn 1 | Turn 2 |
|---|---|---|
| connect + `session.update` handshake before audio is sent | 1.7 s | 1.0 s |
| recognition after the commit until the transcript is final | ~1.2 s | ~1.9 s |
| **waiting for the WebSocket close after the transcript is final** | **~1.9 s** | **~1.9 s** |

## Where the time goes (code)
- `AsrSessions.begin` creates a `LiveTurn` when the finished segment arrives; its worker (`LiveTurn._run`,
  `transcription.py:262`) then connects (`factory()`), sends `session.update` and waits for `session.updated`
  *before* the first audio is sent. That is the 1.0-1.7 s.
- When the transcript is final the loop returns, and the `finally:` block (`transcription.py:340-343`) runs
  `wire.close()` **before** `self.done.set()`. The connection is created with `close_timeout=2` (line 92), so the
  closing handshake can take up to 2 s. `LiveTurn.finish()` waits for `done` (line 219), so saving the transcript
  and everything after it waits for a handshake nobody needs. That is the ~1.9 s.

## Proposal A - the smallest safe change
Signal "result final" first, close afterwards:

```python
finally:
    self.done.set()              # the transcript is final: callers may continue
    if wire is not None:
        with suppress(Exception):
            wire.close()         # the handshake may take up to close_timeout; nobody waits for it
```
Expected gain: about 1.9 s per turn (40% of the ASR stage). No protocol change, no new thread (the worker already
runs the close), no API change. (Rejected alternative: lowering `close_timeout`; it only hides the wait and
abandons the handshake abruptly.)

Risks and how they are handled:
1. One existing test asserts `wire.closed` immediately after `finish()` (`tests/test_transcription.py:106`). Under
   A the close happens a moment later; that assertion becomes `turn.worker.join(2)` then `assert wire.closed`. This is
   a deliberate, visible change of the contract: *finished* no longer implies *socket closed*.
2. A socket may be closing while the next segment already starts. `AsrSessions.begin` closes the previous turn
   (`live.close()`, joins the worker up to 3 s) and caps concurrent turns at 8; at most one extra closing socket per turn
   exists, for under 2 s.
3. Failure paths (`fail(...)`, cancelled, timeouts) take the same `finally`; `done` is set at the same moment as
   before relative to the result, only the close moves behind it. `close()` still sets `cancelled` and joins.
4. Provider side: the socket is closed a moment after the final transcript; no extra audio is sent, so no cost change.

## Proposal B - pre-connect when speech starts (larger, optional, later)
Open the realtime connection when confirmed speech begins (`vad_speech` "start"), so only the commit and the
recognition remain after the segment arrives. Expected gain: the 1.0-1.7 s connect part. Risks: a misfire or
Stopp leaves an unused connection (needs cleanup on Stopp, reset, idle; `idle_timeout` is 20 s); whether an idle
realtime session is billed is unverified; `AsrSessions.begin` must adopt a pre-warmed turn without breaking the
"previous turn must be saved" rule; more states to test (rapid utterances, hold/merge, Stopp). I would only do B
after A is validated, as its own ticket.

## Validation plan for A (before merging)
1. New unit test: a fake wire whose `close()` blocks. `turn.finish()` must return the final transcript while the
   close is still blocked; after releasing it, `turn.worker.join()` completes and `wire.closed` is true. Also: the
   provider-error path and the cancelled path still close the wire; `LiveTurn.close()` still joins.
2. Adjust the one assertion above; run the complete existing suite (about 300 tests, including `AsrSessions`, the
   manual app handlers, the VAD app handler and the demo).
3. Browser: the developer-app fake-microphone tests (`test_fake_microphone.py`) and the demo suites, unchanged.
4. Real services (opt-in, needs your approval, about $0.006): re-run `tests/demo/live/test_e2e_smoke.py`; expect the
   ASR stage to drop from ~4.8 s to ~3.0 s, identical transcripts, and the gap between `asr.completed` and the stage
   end below 0.2 s.
5. Manual check by you on the developer apps: 7860 record, Stop, "Abschließen & speichern" (transcript
   `completed`, files complete); 7861 voice test one utterance; afterwards `ss -tn | grep 443` shows the
   connections to OpenAI gone within a few seconds.
6. Rollback: a single small commit, revert if anything differs.

## Decision requested
Approve A (tiny, measurable)? And whether B should become a separate ticket. Until then `riva-42l` stays open and
no shared behavior changes.
