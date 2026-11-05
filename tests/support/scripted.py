"""Turn the conversation fixtures' expectations into scripted LLM replies (deterministic)."""

import json
from typing import Any


def passage_id(turn_index: int) -> str:
    return f"p{turn_index}"


def llm_text_for(turn: dict[str, Any], turn_index: int) -> str:
    """The JSON a good model would return for this fixture turn."""
    expect = turn["expect"]
    op_type = expect["journal_op"]
    spoken = " ".join(segment["asr_text"] for segment in turn["segments"])
    reply = "Danke, dass du mir das erzählst. Das war bestimmt nicht leicht."
    vocabulary = None
    if expect.get("reply_has_question"):
        reply = "Das klingt interessant. Magst du mir noch mehr davon erzählen?"
    if word := expect.get("vocabulary_word"):
        reply = f"Kennst du das Wort {word}? So heißt die lange Nase. Was hat der Elefant gemacht?"
        vocabulary = {"word": word, "explanation": "die lange Nase des Elefanten"}
    if expect.get("clarification_needed") and not expect.get("reply_has_question"):
        reply = "Meinst du das so? Magst du es mir noch einmal sagen?"
    op = {
        "type": op_type,
        "text": turn["intended"] if op_type in ("append", "revise") else "",
        "target_id": passage_id(expect["revise_target_turn"]) if op_type == "revise" else None,
        "evidence": spoken if op_type == "revise" else None,
    }
    return json.dumps(
        {
            "spoken_reply": reply,
            "journal_op": op,
            "uncertain": bool(expect.get("uncertain", False)),
            "clarification_needed": bool(expect.get("clarification_needed", False)),
            "edits": [],
            "added_facts": [],
            "vocabulary": vocabulary,
            "flags": list(expect.get("flags", [])),
        },
        ensure_ascii=False,
    )
