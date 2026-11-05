"""Participant page: large controls, one status indicator, journal/heard tabs, nothing technical.

All visible text comes from ``riva.demo.texts`` (German). Technical read-outs sit in a closed
spoiler. The page is built on top of the proven VAD capture and Web Audio player; its hidden
controls (``visible="hidden"``) stay in the DOM so the page scripts can click them.
"""

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from riva.audio.recordings import SessionIdentity, new_development_session
from riva.demo import texts
from riva.demo.session import DemoSession
from riva.experiments.asr_stream import VAD_BROWSER_JS

if TYPE_CHECKING:
    import gradio as gr

    from riva.demo.app import DemoRuntime

HEAD = (
    "<script>document.documentElement.lang = 'de';"
    " if (window.matchMedia('(prefers-color-scheme: dark)').matches"
    " && !location.search.includes('__theme'))"
    " { location.replace(location.pathname + '?__theme=light'); }</script>"
    '<meta name="color-scheme" content="light">'
)  # the colors are designed for the light theme; a dark system setting must not change them

CSS = """
.gradio-container { max-width: 960px !important; margin: 0 auto; font-size: 1.25rem; }
h1 { font-size: 2.6rem !important; text-align: center; }
#riva-intro { padding: 1rem 1.4rem; border-radius: 1rem; background: #eff6ff; color: #111827;
  border: 3px solid #1d4ed8; font-size: 1.4rem; }
#riva-intro h2 { font-size: 1.8rem; margin: 0 0 0.4rem; }
#riva-intro ol { margin: 0.2rem 0 0.6rem 1.6rem; line-height: 1.6; }
#riva-intro p { margin: 0; font-weight: 600; }
#riva-status {
  display: flex; align-items: center; justify-content: center; gap: 1.2rem;
  padding: 1.2rem; border-radius: 1.2rem; border: 4px solid #1f2937;
  background: #f3f4f6; color: #111827; min-height: 9rem;
}
#riva-status-emoji { font-size: 5rem; line-height: 1; }
#riva-status-text { font-size: 2.2rem; font-weight: 700; }
#riva-working { display: none; height: 10px; margin-top: 0.6rem; border-radius: 5px;
  background: linear-gradient(90deg, #a16207 0 30%, #fde68a 30% 100%); background-size: 200% 100%;
  animation: riva-slide 1.4s linear infinite; }
#riva-status[data-working="true"] + #riva-working { display: block; }
@keyframes riva-slide { from { background-position: 0 0; } to { background-position: -200% 0; } }
@media (prefers-reduced-motion: reduce) { #riva-working { animation: none; } }
#riva-status-note { font-size: 1.3rem; min-height: 1.8rem; text-align: center; color: #374151;
  margin: 0.4rem 0 0; }
#riva-status[data-phase="stopped"] { background: #e5e7eb; border-color: #374151; }
#riva-status[data-phase="hearing"] { background: #dbeafe; border-color: #1d4ed8; }
#riva-status[data-phase="speaking"] { background: #dcfce7; border-color: #15803d; }
#riva-status[data-phase="thinking"], #riva-status[data-phase="preparing"],
#riva-status[data-phase="transcribing"] { background: #fef9c3; border-color: #a16207; }
#riva-status[data-phase="error"], #riva-status[data-phase="mic_blocked"] {
  background: #fee2e2; border-color: #b91c1c; }
#riva-start button, #riva-stop button, button#riva-start, button#riva-stop {
  min-height: 5.5rem; font-size: 2rem !important; font-weight: 700; border-radius: 1rem; }
#riva-journal textarea, #riva-heard textarea, #riva-reply textarea {
  font-size: 1.5rem !important; line-height: 1.5; }
button#riva-start { background: #166534 !important; color: #ffffff !important;
  border: 3px solid #14532d !important; }
button#riva-stop { background: #b91c1c !important; color: #ffffff !important;
  border: 3px solid #7f1d1d !important; }
[role="tab"] { color: #111827 !important; }
.info-text, .subdued { color: #374151 !important; }
[role="tab"].selected, [role="tab"][aria-selected="true"] {
  color: #111827 !important; font-weight: 700; border-bottom: 5px solid #1d4ed8 !important; }
/* Gradio's JSON viewer colors fail WCAG contrast; found by axe once the timeline has content. */
.json-node .key { color: #7c2d12 !important; }
.json-node .value.string { color: #14532d !important; }
.json-node .value.number { color: #1e3a8a !important; }
.json-node .value.boolean, .json-node .value.null { color: #581c87 !important; }
button:focus-visible, textarea:focus-visible, [role="tab"]:focus-visible, summary:focus-visible {
  outline: 5px solid #1d4ed8 !important; outline-offset: 3px !important; }
[role="tab"] { font-size: 1.35rem !important; padding: 0.8rem 1.2rem !important; }
"""

INTRO_HTML = (
    '<section id="riva-intro" aria-labelledby="riva-intro-title"><h2 id="riva-intro-title">'
    + texts.INTRO_TITLE
    + "</h2><ol>"
    + "".join(f"<li>{step}</li>" for step in texts.INTRO_STEPS)
    + f"</ol><p>{texts.INTRO_NOTE}</p></section>"
)

STATUS_HTML = (
    '<div id="riva-status" role="status" aria-live="polite" data-phase="idle" data-working="false">'
    '<span id="riva-status-emoji" aria-hidden="true">'
    + texts.PHASES["idle"][0]
    + '</span><span id="riva-status-text">'
    + texts.PHASES["idle"][1]
    + "</span></div>"
    '<div id="riva-working" aria-hidden="true"></div>'
    '<p id="riva-status-note" role="status" aria-live="polite"></p>'
)

# Start: remember the click (it also unlocks audio), clear a previous Stopp, then start or resume
# the browser VAD (see asr_stream). The hidden inputs are returned unchanged for the Python handler.
START_JS = (
    "async (enabled, identity) => { window.rivaDemoClicked = true; window.rivaStopped = false;"
    " await (" + VAD_BROWSER_JS.strip() + ")(enabled); return [enabled, identity]; }"
)

# Remember this page's session id and tell the server when the page goes away (reload/close), so
# the old session is closed at once instead of lingering until it expires.
SESSION_JS = """
(id) => {
  if (!id) return;
  window.rivaSessionId = id;
  if (window.rivaUnloadBeacon) return;
  window.rivaUnloadBeacon = true;
  window.addEventListener('pagehide', () => {
    navigator.sendBeacon('/riva/session_end',
      new Blob([JSON.stringify({session_id: window.rivaSessionId})], {type: 'application/json'}));
  });
}
"""
# Stopp (or "Alles anhalten" from the presenter) bumps a counter; a change stops the player at once.
STOP_SEQ_JS = """
(seq) => {
  if (window.rivaStopSeq !== undefined && window.rivaStopSeq !== seq && window.rivaStopAll)
    window.rivaStopAll();
  window.rivaStopSeq = seq;
}
"""
# Plays the cached preview sentence of a voice (a click on the dropdown is a user gesture).
PREVIEW_JS = """
(() => {
  window.rivaPreviewPlays = 0;
  let lastVoice = '', lastAt = 0;
  window.rivaPlayPreview = (voice) => {
    // Gradio can report a choice twice, or an empty value when the list opens: play each once.
    if (typeof voice !== 'string' || !voice) return;
    const now = Date.now();
    if (voice === lastVoice && now - lastAt < 1500) return;
    lastVoice = voice;
    lastAt = now;
    if (window.rivaStopSpeech) window.rivaStopSpeech();
    if (window.rivaPreviewAudio) { try { window.rivaPreviewAudio.pause(); } catch (_) {} }
    const audio = new Audio('/riva/voice_preview/' + encodeURIComponent(voice));
    window.rivaPreviewAudio = audio;
    window.rivaPreviewPlays += 1;
    audio.play().catch(() => {});
  };
})();
"""
STOP_JS = "(identity) => { if (window.rivaStopAll) window.rivaStopAll(); return [identity]; }"

# Computes the visible phase from the server phase plus what only the browser knows.
INDICATOR_JS = """
(() => {
  if (window.rivaIndicatorInstalled) return;
  window.rivaIndicatorInstalled = true;
  const PHASES = __PHASES__;
  const WAIT_NOTE = __WAIT_NOTE__;
  window.rivaServerPhase = 'idle';
  let shown = '', closedSince = 0;
  const compute = () => {
    const server = window.rivaServerPhase || 'idle';
    const vad = window.rivaVad || {};
    const stages = vad.stages || [];
    const last = stages[stages.length - 1] || '';
    if (server === 'closed') return 'closed';
    if (last.startsWith('FAILED')) return 'mic_blocked';
    if (window.rivaStopped || server === 'stopped') return 'stopped';
    if (!window.rivaDemoClicked) return 'idle';
    if (!stages.includes('listening')) return 'starting';
    if (vad.speaking) return 'hearing';
    if (window.rivaIsPlaying && window.rivaIsPlaying()) return 'speaking';
    if (server === 'idle') return 'listening';
    return server;
  };
  // Stopp: end whatever is happening, in the browser as well: playback, microphone capture and the
  // utterance being recorded. The server is told separately (button handler / stop counter).
  window.rivaStopAll = () => {
    window.rivaStopped = true;
    if (window.rivaStopSpeech) window.rivaStopSpeech();
    if (window.rivaVad && window.rivaVad.discardAndPause) window.rivaVad.discardAndPause();
  };
  const WORKING = ['starting', 'transcribing', 'thinking', 'preparing'];
  let phaseSince = Date.now();
  setInterval(() => {
    const phase = compute();
    const box = document.getElementById('riva-status');
    if (!box) return;
    if (phase === 'closed') {
      closedSince = closedSince || Date.now();
      if (Date.now() - closedSince > 2500) location.reload();
    }
    const note = document.getElementById('riva-status-note');
    if (phase !== shown) {
      shown = phase;
      phaseSince = Date.now();
      const [emoji, text] = PHASES[phase] || PHASES.idle;
      box.dataset.phase = phase;
      box.dataset.working = WORKING.includes(phase) ? 'true' : 'false';
      document.getElementById('riva-status-emoji').textContent = emoji;
      document.getElementById('riva-status-text').textContent = text;
      if (note) note.textContent = '';
    }
    // A long wait gets a calm explanation so nobody thinks the page froze.
    if (note && WORKING.includes(phase) && Date.now() - phaseSince > 4000 && !note.textContent) {
      note.textContent = WAIT_NOTE;
    }
  }, 120);
})();
""".replace("__PHASES__", json.dumps(texts.PHASES, ensure_ascii=False)).replace(
    "__WAIT_NOTE__", json.dumps(texts.WAIT_NOTE, ensure_ascii=False)
)


def _values(session: DemoSession, timeline: list[dict[str, Any]]) -> tuple[Any, ...]:
    view = session.view()
    return (
        view["journal"],
        view["heard"],
        view["reply"],
        timeline,
        view["phase"],
        view["generation"],
        view["session_id"],
        view["stop_seq"],
    )


def build_participant_app(runtime: "DemoRuntime") -> "gr.Blocks":
    import gradio as gr

    asr, replies = runtime.asr, runtime.replies

    def poll(identity: SessionIdentity) -> tuple[Any, ...]:
        session = runtime.ensure(identity)
        return _values(session, asr.get_vad_diagnostics(identity))

    def vad_segment(payload: str, identity: SessionIdentity) -> Any:
        session = runtime.ensure(identity)
        token = session.token()  # a Stopp or reset bumps the epoch: this token is then stale
        session.set_phase("transcribing", token)

        def view() -> tuple[Any, ...]:
            return _values(session, asr.get_vad_diagnostics(identity))

        try:
            segment = session.segment_updates(payload, token)
            outcome = None
            try:
                while True:
                    next(segment)
                    yield view()
            except StopIteration as stop:
                outcome = stop.value
            if outcome is None:
                if session.is_current(token):  # a stopped/reset stage is not an error
                    session.show_error("segment_not_processed")
                yield view()
                return
            result = session.on_segment(outcome, token)
            if result == "held":
                session.set_phase("listening", token)
            elif result == "unusable":
                if session.is_current(token):
                    session.show_error("asr_unusable")
            elif result is not None:
                session.set_phase("preparing", token)
                yield view()
                if result.run is not None:
                    for _ in result.run.updates():
                        yield view()
                    if result.run.status == "failed" and session.is_current(token):
                        session.show_error("reply_failed")
                    elif session.phase == "preparing":
                        session.set_phase("listening", token)
                elif session.is_current(token):
                    session.show_error("reply_not_started")
            yield view()
        except Exception as error:  # fixed code only; the message may contain private text
            session.show_error(f"handler_{type(error).__name__}")
            yield view()

    def vad_speech(speech_payload: str, identity: SessionIdentity) -> None:
        try:
            data = json.loads(speech_payload)
            if not isinstance(data, dict) or set(data) != {"kind", "at"}:
                raise ValueError("invalid_speech_event")
            at = data["at"]
            if isinstance(at, bool) or not isinstance(at, (int, float)):
                raise ValueError("invalid_speech_event")
            asr.note_speech(identity, str(data["kind"]), float(at))
        except (ValueError, TypeError):
            return
        if data["kind"] == "start":
            session = runtime.ensure(identity)
            session.note_user_speech_started()
            replies.cancel_generating(identity)
            if session.phase == "error":
                session.set_phase("listening")

    def set_voice(voice: str, identity: SessionIdentity) -> None:
        session = runtime.ensure(identity)
        # Gradio may report one choice more than once: only a real change is applied (and logged).
        if (
            voice in runtime.config.voices
            and not session.closed
            and voice != session.settings.voice
        ):
            session.update_settings(replace(session.settings, voice=voice))

    def resume(enabled: bool, identity: SessionIdentity) -> None:
        runtime.ensure(identity).resume()

    def stop(identity: SessionIdentity) -> tuple[Any, ...]:
        session = runtime.ensure(identity)
        session.interrupt("participant_stop")
        return _values(session, asr.get_vad_diagnostics(identity))

    app = gr.Blocks(title=texts.PAGE_TITLE, analytics_enabled=False)
    with app:
        identity = gr.State(
            value=new_development_session, time_to_live=3600, delete_callback=runtime.release
        )
        gr.Markdown(f"# {texts.TITLE}\n\n{texts.SUBTITLE}")
        gr.HTML(INTRO_HTML)
        gr.HTML(STATUS_HTML)
        with gr.Row():
            start = gr.Button(texts.START, variant="primary", size="lg", elem_id="riva-start")
            stop_button = gr.Button(texts.STOP, variant="stop", size="lg", elem_id="riva-stop")
        voice_choices = [(name.capitalize(), name) for name in runtime.config.voices]
        voice = gr.Dropdown(
            voice_choices, value=runtime.defaults.voice, label=texts.VOICE_LABEL,
            info=texts.VOICE_INFO, elem_id="riva-voice", filterable=False,
        )  # fmt: skip
        reply = gr.Textbox(
            label=texts.REPLY_LABEL, interactive=False, lines=3, elem_id="riva-reply",
            show_label=True,
        )  # fmt: skip
        with gr.Tabs():
            with gr.Tab(texts.TAB_JOURNAL):
                journal = gr.Textbox(
                    label=texts.JOURNAL_LABEL, interactive=False, lines=10,
                    placeholder=texts.JOURNAL_EMPTY, elem_id="riva-journal",
                )  # fmt: skip
            with gr.Tab(texts.TAB_HEARD):
                heard = gr.Textbox(
                    label=texts.HEARD_LABEL, interactive=False, lines=10,
                    placeholder=texts.HEARD_EMPTY, elem_id="riva-heard",
                )  # fmt: skip
        with gr.Accordion(texts.TECH_LABEL, open=False):
            gr.Markdown("Mikrofon: wartet …", elem_id="riva-vad-state")
            timeline = gr.JSON(label=texts.TECH_TIMELINE_LABEL)

        # Hidden plumbing the page scripts click or read (kept in the DOM).
        enabled = gr.Checkbox(value=True, visible="hidden")
        payload = gr.Textbox(visible=False)
        submit = gr.Button("-", visible="hidden", elem_id="riva-vad-submit")
        speech_payload = gr.Textbox(visible=False)
        speech = gr.Button("-", visible="hidden", elem_id="riva-vad-speech")
        phase_box = gr.Textbox(visible="hidden", elem_id="riva-phase")
        generation = gr.Textbox(visible="hidden", elem_id="riva-generation-id")
        session_box = gr.Textbox(visible="hidden", elem_id="riva-session-id")
        stop_box = gr.Textbox(visible="hidden", elem_id="riva-stop-seq")
        ticker = gr.Timer(0.3)

        outputs = [journal, heard, reply, timeline, phase_box, generation, session_box, stop_box]
        start.click(
            resume, inputs=[enabled, identity], outputs=[], js=START_JS, queue=False,
            show_progress="hidden", api_visibility="private",
        )  # fmt: skip
        stop_button.click(
            stop, inputs=[identity], outputs=outputs, js=STOP_JS, queue=False,
            show_progress="hidden", api_visibility="private",
        )  # fmt: skip
        submit.click(
            vad_segment, inputs=[payload, identity], outputs=outputs,
            js="(payload, identity) => [window.rivaVad.activePayload || '', identity]",
            concurrency_limit=4, show_progress="hidden", api_visibility="private",
        ).then(
            None, js=(
                "() => { if (!window.rivaVad) return; window.rivaVad.activePayload = null;"
                " window.rivaVad.busy = false;"
                " if (window.rivaVad.submitNext) window.rivaVad.submitNext(); }"
            ), queue=False, api_visibility="private",
        )  # fmt: skip
        speech.click(
            vad_speech, inputs=[speech_payload, identity], outputs=[],
            js=(
                "(payload, identity) => "
                "[JSON.stringify(window.rivaVad.speechEvents.shift() || {}), identity]"
            ),
            queue=False, show_progress="hidden", api_visibility="private",
        )  # fmt: skip
        ticker.tick(
            poll, inputs=[identity], outputs=outputs, queue=False, show_progress="hidden",
            api_visibility="private",
        )  # fmt: skip
        phase_box.change(
            None, inputs=[phase_box], js="(p) => { window.rivaServerPhase = p; }", queue=False,
            api_visibility="private",
        )  # fmt: skip
        voice.input(
            set_voice, inputs=[voice, identity], outputs=[],
            js=(
                "(voice, identity) => { if (window.rivaPlayPreview) window.rivaPlayPreview(voice);"
                " return [voice, identity]; }"
            ),
            queue=False, show_progress="hidden", api_visibility="private",
        )  # fmt: skip
        stop_box.change(
            None, inputs=[stop_box], js=STOP_SEQ_JS, queue=False, api_visibility="private"
        )
        session_box.change(
            None, inputs=[session_box], js=SESSION_JS, queue=False, api_visibility="private"
        )
        app.load(None, js=f"() => {{ {PREVIEW_JS} }}", queue=False, api_visibility="private")
        app.load(None, js=f"() => {{ {INDICATOR_JS} }}", queue=False, api_visibility="private")
    return app
