"""Small German text helpers: language check and tolerant word matching (no extra dependencies)."""

import re
import unicodedata


def _vocabulary(text: str) -> frozenset[str]:
    return frozenset(text.split())


GERMAN_STOPWORDS = _vocabulary(
    """der die das den dem des ein eine einen einem einer und oder aber ist sind war waren bin bist
    habe hast hat haben ich du er sie es wir ihr nicht auch mit von zu im in am an auf aus bei für
    nach wie was wo wer warum dann dort hier sehr schon noch mal gern gerne ja nein kein keine mein
    dein heute gestern wenn weil dass wirklich"""
)
ENGLISH_STOPWORDS = _vocabulary(
    """the a an and or but is are was were am be been have has had i you he she it we they not with
    of to in on at for from as how what where who why then there here very just also my your this
    that because if do does did"""
)
NUMBER_WORDS = {
    "null": "0", "eins": "1", "ein": "1", "eine": "1", "einen": "1", "zwei": "2", "drei": "3",
    "vier": "4", "fünf": "5", "sechs": "6", "sieben": "7", "acht": "8", "neun": "9", "zehn": "10",
}  # fmt: skip
_SUFFIXES = ("ern", "er", "en", "em", "es", "e", "n", "s")
_WORD = re.compile(r"[a-zA-ZäöüÄÖÜß0-9]+")


def words(text: str) -> list[str]:
    return [match.group(0) for match in _WORD.finditer(text)]


def looks_german(text: str) -> bool:
    """Heuristic: reject text that is clearly English; accept short texts that show no English.

    Short passages ("Große graue Elefanten.") may contain no function words at all, so a German
    stopword is only required once the text is long enough to expect one.
    """
    tokens = [word.casefold() for word in words(text)]
    german = sum(token in GERMAN_STOPWORDS for token in tokens)
    english = sum(token in ENGLISH_STOPWORDS for token in tokens)
    if len(tokens) < 6:
        return english <= german
    return german >= 1 and german > english


def normalize(word: str) -> str:
    """Fold case/umlauts, map number words to digits, strip common inflection endings."""
    folded = word.casefold()
    if folded in NUMBER_WORDS:
        return NUMBER_WORDS[folded]
    if folded.isdigit():
        return folded
    folded = unicodedata.normalize("NFKD", folded.replace("ß", "ss"))
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    for suffix in _SUFFIXES:
        if len(folded) > len(suffix) + 3 and folded.endswith(suffix):
            return folded[: -len(suffix)]
    return folded


def _within_one_edit(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) <= 1
    short, long = sorted((a, b), key=len)
    return any(long[:i] + long[i + 1 :] == short for i in range(len(long)))


def same_word(a: str, b: str) -> bool:
    """Tolerant equality: inflection, umlauts, number words/digits, one typo (words >= 4 chars)."""
    na, nb = normalize(a), normalize(b)
    return na == nb or (min(len(na), len(nb)) >= 4 and _within_one_edit(na, nb))


def grounding_score(passage: str, source: str) -> float:
    """Share of the passage's content words that can be traced to the source text (0..1).

    A soft signal for the presenter and for evals - never a gate: legitimate repairs (ASR
    homophones, pronoun resolution, filler removal) legitimately lower it.
    """
    stop = GERMAN_STOPWORDS
    content = [
        w for w in words(passage) if w.casefold() not in stop and (len(w) >= 3 or w.isdigit())
    ]
    if not content:
        return 1.0
    source_words = words(source)
    found = sum(any(same_word(word, other) for other in source_words) for word in content)
    return found / len(content)
