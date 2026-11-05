# Manual test guide (hardware and usability check)

Use **Chrome** on the machine that has the microphone and speakers, and open the pages as `localhost` (the
microphone only works on localhost/https). A headset avoids speaker echo; run it once more with speakers to learn
whether echo is a problem. Real services are billed by OpenAI per use (a few cents for this whole checklist).

## Start the servers (from `/home/rmary/projects/riva/riva-basic`)

| What | Command | URL |
|---|---|---|
| **Demo, real services** | `uv run --env-file .env riva-demo --port 7863` | participant http://localhost:7863/ · presenter http://localhost:7863/presenter/ |
| Demo, practice without cost (fake ASR/LLM/TTS) | `uv run riva-demo --fake --port 7864` | http://localhost:7864/ · http://localhost:7864/presenter/ |
| Developer: manual recording + ASR | `uv run --env-file .env riva-asr-test --private-dir ../private --port 7860` | http://localhost:7860/ |
| Developer: continuous voice (VAD + LLM + TTS) | `uv run --env-file .env riva-voice-test --private-dir ../private --port 7861` | http://localhost:7861/ |
| Developer: synthetic harness (no microphone, no cost) | `uv run riva-voice-test --vad-self-test --private-dir ../private --port 7862` | http://localhost:7862/ |

Stop a server with Ctrl+C. Logs of the background servers started for you are in `../private/logs/`.
Recordings, transcripts and journals land in `../private/research-data/` (never in Git).

## 1. Presenter: before the demo (`/presenter/`)
- [ ] **Automatisch prüfen**: archive folder, key, models, speech recognition are ✅ (a ⚠️ for the voice-preview cache is fine).
- [ ] **Mikrofon prüfen**: allow the microphone; the line says it is allowed.
- [ ] **Testton abspielen** and confirm you hear it on the intended speakers/headset.
- [ ] (costs a few cents) tick the cost box, **Alle Stimmen vorbereiten**, then **Stimmtest** for the voice you will use.

## 2. Participant (`/`) - one normal conversation
- [ ] The introduction "So geht's" is visible, nothing plays, no microphone prompt yet. Buttons are large and readable.
- [ ] **▶ Start**, allow the microphone: status 👂 "Ich höre dir zu."
- [ ] Say: *"Heute war ich im Zoo."* then pause. Watch the indicator: 🗣️ → ✍️ → 🤔 → ⏳ → 🔊. After about 4 s of waiting the note "Das dauert einen Moment" appears.
- [ ] The reply is spoken, in German, short, with a question. The journal tab shows your sentence; the tab "Das habe ich gehört" shows the raw text.
- [ ] Answer the question (*"Elefanten und einen ganz großen Löwen."*): a second journal paragraph appears, and the reply refers to what you said.
- [ ] Correct yourself: *"Nein, es waren drei Elefanten."* The old entry changes only if it was unambiguous; otherwise the assistant asks.
- [ ] Say something unclear on purpose (mumble, or a half sentence): it asks again instead of inventing.
- [ ] **Stimme** dropdown: choose another voice - the preview sentence plays **once** per choice (each new voice costs one tiny TTS request the first time); the next reply uses that voice. In `../private/logs/demo.log` every `tts.requested` line shows the voice in `detail`.
- [ ] Dark system theme (if you use it): the page stays light. Tab/Enter reach Start and Stopp; focus is clearly visible.

## 3. Stopp in every state
- [ ] While you are **speaking**: press **⏹ Stopp** - "✋ Gestoppt", nothing is answered afterwards, then **▶ Start** listens again.
- [ ] While it is **thinking/writing** (✍️/🤔): Stopp - no reply ever appears and the journal does not change; Start works again.
- [ ] While the **voice is prepared** (⏳): Stopp - no sound starts.
- [ ] While the reply is **audible** (🔊): Stopp - sound stops immediately. The earlier journal entry stays.
- [ ] Speak while the assistant talks: it stops talking (barge-in), your sentence is processed. Cough briefly: no harm.

## 4. Presenter during the conversation
- [ ] The sessions table shows the session ("aktiv"); choose it: journal, heard text and timeline update; no content outside the presenter page.
- [ ] Change **Persona** (e.g. Freundlicher Begleiter) and press **Auf gewählte Sitzung anwenden**: the next reply's style changes.
- [ ] **Pause in ms** (e.g. 1500) + **Für neue Sitzungen speichern**: applies when a participant presses Start next time.
- [ ] **⏹ Alles anhalten**: the participant page shows "Gestoppt" and is silent. **Gewählte Sitzung beenden (Neustart)**: the participant page restarts with an empty journal.
- [ ] Reload the participant page: a new empty session starts; the old one shows as ended in the presenter table.

## 5. Developer applications (check that the ASR change did not break them)
- [ ] **7860** (manual): *Aufnahme starten*, speak a sentence, stop, **Abschließen & speichern**: the final transcript appears and the files exist under `../private/research-data/…`.
- [ ] **7861** (continuous voice): the stage line reaches `listening`; speak a sentence; a transcript, a reply text and spoken audio follow. Speak during the reply: it stops.
- [ ] **7862** (harness, no microphone): *Run synthetic pipeline test* shows the full timeline and a reply.
- [ ] After using any app, `ss -tn | grep ':443'` shows no lingering connections to OpenAI after a few seconds.

## 6. Afterwards
- Delete test sessions: `docs/demo/README.md`, section "Deleting a session and its recordings".
- Please note: microphone/speaker model, whether echo occurred, how long a reply felt to take after you stopped speaking, anything confusing in German wording, anything a participant could press by mistake.
- The demo is **not** presentation-ready until you have done this check.
