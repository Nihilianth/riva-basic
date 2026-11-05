"""Presenter page (/presenter): checks before the demo, settings, live view, reset and stop.

Everything is German. The page belongs to the person running the demo on the same machine; it
shows session content (journal, transcripts, timeline) and offers the controls the participant
page must not have. The participant page is untouched by anything here except through the
runtime (new-session defaults, pause threshold, resets).
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from riva.demo import preflight, texts
from riva.demo.session import DemoSession, SessionSettings

if TYPE_CHECKING:
    import gradio as gr

    from riva.demo.app import DemoRuntime

TITLE = "Vorführenden-Seite"
SESSION_HEADERS = ["Sitzung", "Zustand", "Anzeige", "Persona", "Modell", "Stimme",
                   "Einträge", "Alter (s)", "Hinweise"]  # fmt: skip
CHECK_HEADERS = ["Gruppe", "Prüfung", "Ergebnis", "Hinweis"]
ALERT_TEXT = {"possible_distress": "Mögliche Belastung gemeldet"}
MIC_JS = """
async () => {
  const out = document.getElementById('pf-mic-result');
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    stream.getTracks().forEach((track) => track.stop());
    out.textContent = '✅ Das Mikrofon ist erlaubt.';
  } catch (error) {
    out.textContent = '❌ Kein Zugriff auf das Mikrofon (' +
      String((error && error.name) || 'Fehler').replace(/[^A-Za-z]/g, '') + ').';
  }
}
"""
TONE_JS = """
async () => {
  const out = document.getElementById('pf-tone-result');
  const Ctor = window.AudioContext || window.webkitAudioContext;
  const ctx = new Ctor();
  await ctx.resume();
  const oscillator = ctx.createOscillator();
  const gain = ctx.createGain();
  gain.gain.value = 0.15;
  oscillator.frequency.value = 440;
  oscillator.connect(gain).connect(ctx.destination);
  oscillator.start();
  oscillator.stop(ctx.currentTime + 0.7);
  out.textContent = '🔊 Der Ton läuft … Hast du ihn gehört?';
  window.rivaToneContext = ctx;
}
"""
HEARD_JS = (
    "() => { document.getElementById('pf-tone-result').textContent = '✅ Der Ton wurde gehört.'; }"
)
UNHEARD_JS = (
    "() => { document.getElementById('pf-tone-result').textContent ="
    " '❌ Kein Ton. Bitte Lautsprecher, Lautstärke und Browser-Tab prüfen.'; }"
)
CSS = """
.gradio-container { max-width: 1200px !important; margin: 0 auto; }
button:focus-visible, textarea:focus-visible, [role="tab"]:focus-visible {
  outline: 4px solid #1d4ed8 !important; outline-offset: 2px !important; }
button#pf-run { background: #166534 !important; color: #ffffff !important; }
button#act-stop { background: #b91c1c !important; color: #ffffff !important; }
.min_value, .max_value, .subdued { color: #374151 !important; }
#pf-mic-result, #pf-tone-result { font-size: 1.1rem; font-weight: 600; min-height: 1.6rem; }
"""
HEAD = "<script>document.documentElement.lang = 'de';</script>"
MIC_IDLE = (
    '<p id="pf-mic-result" role="status" aria-live="polite">Mikrofon: noch nicht geprüft.</p>'
)
TONE_IDLE = (
    '<p id="pf-tone-result" role="status" aria-live="polite">Testton: noch nicht geprüft.</p>'
)


def short_id(session: DemoSession) -> str:
    return session.identity.session_id[:8]


def _persona_label(runtime: "DemoRuntime", persona_id: str) -> str:
    return runtime.config.persona(persona_id).label


def timeline_tail(session: DemoSession, limit: int = 40) -> list[dict[str, Any]]:
    """The latest timeline events of the session archive (private content, presenter only)."""
    path = session.archive.directory / "timeline.jsonl"
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(line) for line in lines]


def alerts_markdown(session: DemoSession) -> str:
    alerts = [e for e in timeline_tail(session, 500) if e.get("event") == "presenter_alert"]
    if not alerts:
        return "Keine Hinweise."
    return "\n".join(
        f"- ⚠️ {ALERT_TEXT.get(str(a.get('kind')), 'Hinweis')} ({a['t'][11:19]})" for a in alerts
    )


def session_rows(runtime: "DemoRuntime") -> list[list[Any]]:
    rows = []
    for session in sorted(runtime.sessions.values(), key=lambda s: s.created_at, reverse=True):
        view = session.view()
        state = "aktiv" if not session.closed else f"beendet ({session.close_reason})"
        alerts = sum(1 for e in timeline_tail(session, 500) if e.get("event") == "presenter_alert")
        rows.append([
            short_id(session), state, texts.PHASES.get(view["phase"], ("", view["phase"]))[1],
            _persona_label(runtime, session.settings.persona_id), session.settings.model,
            session.settings.voice, len(session.journal.passages),
            round(runtime.monotonic() - session.created_at), alerts,
        ])  # fmt: skip
    return rows


def session_choices(runtime: "DemoRuntime") -> list[tuple[str, str]]:
    ordered = sorted(runtime.sessions.values(), key=lambda s: (s.closed, -s.created_at))
    return [
        (f"{short_id(s)} ({'aktiv' if not s.closed else 'beendet'})", s.identity.session_id)
        for s in ordered
    ]


def session_detail(runtime: "DemoRuntime", session_id: str | None) -> tuple[str, str, Any, str]:
    session = runtime.get(session_id) if session_id else None
    if session is None:
        return "", "", [], "Keine Sitzung ausgewählt."
    view = session.view()
    return view["journal"], view["heard"], timeline_tail(session), alerts_markdown(session)


def _settings_from(runtime: "DemoRuntime", persona: str, model: str, voice: str) -> SessionSettings:
    settings = SessionSettings(persona, model, voice)
    runtime.config.check_choice(settings.persona_id, settings.model, settings.voice)
    return settings


def build_presenter_app(runtime: "DemoRuntime") -> "gr.Blocks":
    import gradio as gr

    config = runtime.config
    probe = runtime.probe
    personas = [(p.label, p.id) for p in config.personas]
    models = [m.id for m in config.models]

    def run_checks() -> list[list[str]]:
        if probe is None:
            return preflight.as_rows(preflight.interactive_checks())
        checks = preflight.run_automatic(config, probe, runtime.archive_root)
        if runtime.voices is not None:
            checks.append(
                preflight.voice_cache_check(runtime.voices.cached_count(), len(config.voices))
            )
        return preflight.as_rows(checks + preflight.interactive_checks())

    def prepare_voices(confirmed: bool) -> str:
        if runtime.voices is None or not confirmed:
            return "Bitte zuerst bestätigen, dass die Kosten (wenige Cent) in Ordnung sind."
        try:
            made, had = runtime.voices.prepare_all(confirmed=True)
        except Exception:
            return "❌ Nicht alle Stimmen konnten erzeugt werden. Bitte später noch einmal."
        return f"✅ {made} neu erzeugt, {had} waren schon vorhanden."

    def voice_test(confirmed: bool, voice: str) -> str:
        if probe is None or not confirmed:
            return "Bitte zuerst bestätigen, dass die Kosten (wenige Cent) in Ordnung sind."
        check = preflight.paid_voice_test(probe, config, voice, confirmed=True)
        return f"{preflight.STATUS_LABELS[check.status]} – {check.detail}"

    def save_defaults(persona: str, model: str, voice: str, pause: int) -> str:
        try:
            runtime.set_defaults(_settings_from(runtime, persona, model, voice))
            runtime.set_pause_ms(int(pause))
        except ValueError:
            return "❌ Diese Auswahl ist nicht erlaubt."
        return (
            "✅ Gespeichert für neue Sitzungen. Die Pause gilt, wenn ein Teilnehmer Start drückt."
        )

    def apply_active(persona: str, model: str, voice: str, session_id: str | None) -> str:
        session = runtime.get(session_id) if session_id else None
        if session is None or session.closed:
            return "Bitte eine aktive Sitzung auswählen."
        try:
            session.update_settings(_settings_from(runtime, persona, model, voice))
        except ValueError:
            return "❌ Diese Auswahl ist nicht erlaubt."
        return "✅ Gilt ab der nächsten Antwort in dieser Sitzung."

    def end_selected(session_id: str | None) -> str:
        if session_id and runtime.reset(session_id):
            return "✅ Sitzung beendet. Die Teilnehmer-Seite startet neu."
        return "Keine aktive Sitzung ausgewählt."

    def end_all() -> str:
        return f"✅ {runtime.reset_all()} Sitzung(en) beendet."

    def stop_all() -> str:
        return f"⏹ Sprache angehalten in {runtime.stop_all()} Sitzung(en)."

    def refresh(selected: str | None) -> tuple[Any, ...]:
        choices = session_choices(runtime)
        valid = {value for _, value in choices}
        keep = selected if selected in valid else (choices[0][1] if choices else None)
        journal, heard, timeline, alerts = session_detail(runtime, keep)
        return (session_rows(runtime), gr.update(choices=choices, value=keep),
                journal, heard, timeline, alerts)  # fmt: skip

    app = gr.Blocks(title=TITLE, analytics_enabled=False)
    with app:
        gr.Markdown(f"# {TITLE}\n\n[Zur Teilnehmer-Seite](/)")
        with gr.Accordion("1. Vor dem Start: Prüfung", open=True):
            gr.Markdown(
                "**Läuft automatisch:** Ordner, Schlüssel, Modelle, Spracherkennung. "
                "**Braucht dich:** Mikrofon und Testton. **Kostet Geld:** der Stimmtest."
            )
            check_button = gr.Button("Automatisch prüfen", variant="primary", elem_id="pf-run")
            check_table = gr.Dataframe(
                headers=CHECK_HEADERS, interactive=False, wrap=True, elem_id="pf-table"
            )
            with gr.Row():
                mic_button = gr.Button("Mikrofon prüfen", elem_id="pf-mic")
                tone_button = gr.Button("Testton abspielen", elem_id="pf-tone")
                heard_button = gr.Button("Ja, Ton gehört", elem_id="pf-heard")
                unheard_button = gr.Button("Nein, kein Ton", elem_id="pf-unheard")
            gr.HTML(MIC_IDLE)
            gr.HTML(TONE_IDLE)
            with gr.Row():
                test_voice = gr.Dropdown(
                    choices=list(config.voices),
                    value=config.default_voice,
                    label="Stimme für den Test",
                )
                confirm = gr.Checkbox(label="Ja, die Kosten (wenige Cent) sind in Ordnung")
                voice_button = gr.Button("Stimmtest (kostet Geld)", elem_id="pf-voice")
            voice_result = gr.Markdown(elem_id="pf-voice-result")
            prepare_button = gr.Button(
                "Alle Stimmen vorbereiten (kostet Geld)", elem_id="pf-prepare"
            )
            prepare_result = gr.Markdown(elem_id="pf-prepare-result")

        with gr.Accordion("2. Einstellungen", open=True):
            with gr.Row():
                persona = gr.Dropdown(personas, value=runtime.defaults.persona_id, label="Persona")
                model = gr.Dropdown(models, value=runtime.defaults.model, label="Sprachmodell")
                voice = gr.Dropdown(
                    list(config.voices), value=runtime.defaults.voice, label="Stimme"
                )
                pause = gr.Slider(500, 3000, value=runtime.pause_ms, step=100, label="Pause in ms")
            with gr.Row():
                save_button = gr.Button("Für neue Sitzungen speichern", elem_id="set-save")
                apply_button = gr.Button("Auf gewählte Sitzung anwenden", elem_id="set-apply")
            settings_result = gr.Markdown(elem_id="set-result")

        with gr.Accordion("3. Sitzungen", open=True):
            with gr.Row():
                stop_button = gr.Button("⏹ Alles anhalten", variant="stop", elem_id="act-stop")
                end_button = gr.Button("Gewählte Sitzung beenden (Neustart)", elem_id="act-end")
                end_all_button = gr.Button("Alle Sitzungen beenden", elem_id="act-end-all")
            action_result = gr.Markdown(elem_id="act-result")
            sessions_table = gr.Dataframe(
                headers=SESSION_HEADERS, interactive=False, elem_id="sessions"
            )
            selected = gr.Dropdown(label="Sitzung ansehen", choices=[], elem_id="sel")
            alerts = gr.Markdown("Keine Sitzung ausgewählt.", elem_id="alerts")
            with gr.Row():
                journal = gr.Textbox(
                    label="Tagebuch", lines=8, interactive=False, elem_id="p-journal"
                )
                heard = gr.Textbox(
                    label="Das habe ich gehört", lines=8, interactive=False, elem_id="p-heard"
                )
            timeline = gr.JSON(label="Ablauf (letzte Ereignisse)")
        ticker = gr.Timer(1.0)

        hide: dict[str, Any] = {
            "queue": False, "show_progress": "hidden", "api_visibility": "private",
        }  # fmt: skip
        check_button.click(run_checks, outputs=[check_table], **hide)
        mic_button.click(None, js=MIC_JS, **hide)
        tone_button.click(None, js=TONE_JS, **hide)
        heard_button.click(None, js=HEARD_JS, **hide)
        unheard_button.click(None, js=UNHEARD_JS, **hide)
        prepare_button.click(prepare_voices, [confirm], [prepare_result], **hide)
        voice_button.click(voice_test, [confirm, test_voice], [voice_result], **hide)
        save_button.click(save_defaults, [persona, model, voice, pause], [settings_result], **hide)
        apply_button.click(
            apply_active, [persona, model, voice, selected], [settings_result], **hide
        )
        stop_button.click(stop_all, outputs=[action_result], **hide)
        end_button.click(end_selected, [selected], [action_result], **hide)
        end_all_button.click(end_all, outputs=[action_result], **hide)
        ticker.tick(
            refresh, [selected],
            [sessions_table, selected, journal, heard, timeline, alerts], **hide,
        )  # fmt: skip
    return app


def presenter_page_path() -> Path:
    return Path("/presenter")
