"""Every participant-visible string, in Einfaches Deutsch. Tests check this module stays German."""

TITLE = "Mein Tagebuch"
SUBTITLE = "Erzähl mir von deinem Tag. Ich schreibe es für dich auf."
INTRO_TITLE = "So geht's"
INTRO_STEPS = (
    "Drücke auf Start.",
    "Erzähl mir von deinem Tag. Ich höre zu.",
    "Ich schreibe auf, was du sagst, und stelle dir eine Frage.",
    "Mit Stopp bin ich still.",
)
INTRO_NOTE = "Ich bin ein Computer-Programm, kein Mensch."
VOICE_LABEL = "Stimme"
VOICE_INFO = "Wähle eine Stimme. Du hörst einen kurzen Satz."
WAIT_NOTE = "Das dauert einen Moment. Ich bin gleich so weit."
START = "▶ Start"
STOP = "⏹ Stopp"
TAB_JOURNAL = "Mein Tagebuch"
TAB_HEARD = "Das habe ich gehört"
JOURNAL_LABEL = "Mein Tagebuch"
HEARD_LABEL = "Das habe ich gehört"
REPLY_LABEL = "Das sage ich"
JOURNAL_EMPTY = "Hier erscheint dein Tagebuch."
HEARD_EMPTY = "Hier erscheint, was ich gehört habe."
TECH_LABEL = "Für Vorführende: Technik"
TECH_STATE_LABEL = "Mikrofon und Wiedergabe"
TECH_TIMELINE_LABEL = "Ablauf"
PAGE_TITLE = "Mein Tagebuch"

# phase -> (emoji, short text). The same table is mirrored into the page script.
PHASES: dict[str, tuple[str, str]] = {
    "idle": ("😴", "Drücke auf Start."),
    "starting": ("⏳", "Ich mache das Mikrofon bereit …"),
    "listening": ("👂", "Ich höre dir zu."),
    "hearing": ("🗣️", "Ich höre dich."),
    "transcribing": ("✍️", "Ich schreibe mit …"),
    "thinking": ("🤔", "Ich denke nach …"),
    "preparing": ("⏳", "Ich bereite meine Antwort vor …"),
    "speaking": ("🔊", "Ich spreche."),
    "stopped": ("✋", "Gestoppt. Drücke auf Start, wenn es weitergehen soll."),
    "error": ("⚠️", "Da ist etwas schiefgelaufen. Bitte sag es noch einmal."),
    "mic_blocked": ("🎤", "Das Mikrofon geht nicht. Bitte frag um Hilfe."),
    "closed": ("🔄", "Diese Sitzung ist zu Ende. Lade die Seite neu."),
}
