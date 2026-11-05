"""Deterministic fakes for tests and the offline ``--fake`` demo; no network, no paid calls."""

import json
from collections import deque
from collections.abc import Iterable

from riva.demo.turn import LLMReply


class ScriptedTurnLLM:
    """Returns queued replies in order (a string is the raw model text, an Exception is raised)."""

    def __init__(self, replies: Iterable[str | Exception]) -> None:
        self.replies = deque(replies)
        self.calls: list[dict[str, object]] = []

    def respond(
        self, instructions: str, user_input: str, *, model: str, timeout: float
    ) -> LLMReply:
        self.calls.append(
            {"instructions": instructions, "input": user_input, "model": model, "timeout": timeout}
        )
        item = self.replies.popleft()
        if isinstance(item, Exception):
            raise item
        return LLMReply(item, input_tokens=len(user_input) // 4, output_tokens=len(item) // 4)


def turn_json(**overrides: object) -> str:
    """A valid turn document (append, no flags); override any top-level field."""
    base: dict[str, object] = {
        "spoken_reply": "Das klingt schön. Was hast du dann gemacht?",
        "journal_op": {
            "type": "append",
            "text": "Ich war im Zoo.",
            "target_id": None,
            "evidence": None,
        },
        "uncertain": False,
        "clarification_needed": False,
        "edits": [],
        "added_facts": [],
        "vocabulary": None,
        "flags": [],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


class EchoTurnLLM:
    """Offline demo LLM: appends the transcript as the passage and asks one fixed question."""

    def respond(
        self, instructions: str, user_input: str, *, model: str, timeout: float
    ) -> LLMReply:
        transcript = json.loads(user_input.rsplit("\n", 1)[-1])
        text = turn_json(
            journal_op={"type": "append", "text": transcript, "target_id": None, "evidence": None}
        )
        return LLMReply(text, len(user_input) // 4, len(text) // 4)


class FakeProbe:
    """Offline service probe for tests and ``--fake``: configurable answers, counts paid calls."""

    def __init__(
        self,
        *,
        key: bool = True,
        models: set[str] | None = None,
        asr: bool | Exception = True,
        down: bool = False,
    ) -> None:
        self.key, self.asr, self.down = key, asr, down
        self.models = models
        self.paid_calls = 0
        self.model_lookups: list[str] = []

    def api_key_present(self) -> bool:
        return self.key

    def model_available(self, model: str) -> bool:
        from riva.demo.preflight import ProbeError

        self.model_lookups.append(model)
        if self.down:
            raise ProbeError
        return self.models is None or model in self.models

    def asr_reachable(self) -> bool:
        from riva.demo.preflight import ProbeError

        if isinstance(self.asr, Exception) or self.down:
            raise ProbeError
        return self.asr

    def synthesize(self, voice: str, tts_model: str, text: str) -> int:
        self.paid_calls += 1
        return 48_000
