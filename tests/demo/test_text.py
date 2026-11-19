"""The free ASR-reliability heuristic (riva-skm.28): flag foreign-script gibberish, nothing else."""

from riva.demo.text import asr_looks_unreliable


def test_normal_german_and_latin_is_never_flagged() -> None:
    for text in (
        "Heute war ich im Zoo.",
        "Ähm,",
        "Ich mag Fußball und Parkour.",
        "Wir waren im Café, das war naïve schön.",  # accented Latin is fine
        "Die Mackteburger Straßenbahn",  # an ASR homophone is still plausible German
        "123 vier fünf",
        "",
        "   ",
    ):
        assert asr_looks_unreliable(text) is False, text


def test_foreign_script_letters_are_flagged() -> None:
    for text in (
        "我  die der Gab es das irgendwelche",  # CJK (seen in a real session)
        "ขัว Sport mit mal ja voll war schönes",  # Thai (seen in a real session)
        "привет Tag",  # Cyrillic
        "κόσμε hallo",  # Greek
    ):
        assert asr_looks_unreliable(text) is True, text
