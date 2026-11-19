"""One conversational turn: build the prompt, call the LLM, validate, retry once, else fall back.

The LLM is an injected ``TurnLLM`` so tests use deterministic fakes. Provider exceptions are reduced
to fixed codes; their text never leaves this module (it may contain prompts or credentials).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from riva.demo.config import DemoConfig
from riva.demo.prompts import TurnContext, build_input, build_instructions
from riva.demo.schema import (
    JournalOp,
    TurnResult,
    TurnResultError,
    ValidatedTurn,
    parse_turn,
    validate_turn,
)

FALLBACK_REPLY = "Das habe ich nicht ganz verstanden. Magst du es noch einmal sagen?"
RETRY_HINT = (
    "Hinweis: Deine letzte Ausgabe war ungültig ({code}). "
    "Antworte jetzt nur mit gültigem JSON genau nach dem vorgegebenen Schema."
)


@dataclass(frozen=True)
class LLMReply:
    """Raw model output plus token usage (for cost estimates); ``text`` is the JSON document."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class TurnLLM(Protocol):
    def respond(
        self, instructions: str, user_input: str, *, model: str, timeout: float
    ) -> LLMReply: ...


@dataclass(frozen=True)
class TurnOutcome:
    """What the session applies (reply + journal operation), from the LLM or a fallback."""

    source: Literal["llm", "fallback"]
    spoken_reply: str
    journal_op: JournalOp
    validated: ValidatedTurn | None
    attempts: int
    error_codes: tuple[str, ...]
    latency_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    user_input: str = ""  # content: for the private archive only, never for the event log
    raw_outputs: tuple[str, ...] = ()  # content: raw model text of each attempt (private archive)

    @property
    def uncertain(self) -> bool:
        return self.validated is None or self.validated.uncertain

    @property
    def flags(self) -> tuple[str, ...]:
        return self.validated.result.flags if self.validated else ()

    @property
    def result(self) -> TurnResult | None:
        return self.validated.result if self.validated else None


class TurnProcessor:
    def __init__(
        self,
        llm: TurnLLM,
        config: DemoConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.llm, self.config, self.clock = llm, config, clock

    def process(
        self,
        context: TurnContext,
        *,
        persona_id: str,
        model: str,
        revisable_ids: frozenset[str] = frozenset(),
    ) -> TurnOutcome:
        started = self.clock()
        instructions = build_instructions(self.config, persona_id)
        user_input = build_input(context)
        codes: list[str] = []
        raw: list[str] = []
        first_input = user_input
        tokens_in = tokens_out = 0
        for attempt in (1, 2):
            try:
                reply = self.llm.respond(
                    instructions,
                    user_input,
                    model=model,
                    timeout=self.config.llm_timeout_seconds,
                )
            except Exception:  # provider/network/timeouts: never keep the message
                codes.append("llm_failed")
                break
            tokens_in += reply.input_tokens
            tokens_out += reply.output_tokens
            raw.append(reply.text)
            try:
                validated = validate_turn(
                    parse_turn(reply.text),
                    context.transcript,
                    revisable_ids=revisable_ids,
                    meta_hint=context.classified == "meta",
                )
            except TurnResultError as error:
                codes.append(error.code)
                user_input = build_input(context) + "\n\n" + RETRY_HINT.format(code=error.code)
                continue
            return TurnOutcome(
                "llm",
                validated.result.spoken_reply.strip(),
                validated.result.journal_op,
                validated,
                attempt,
                tuple(codes),
                (self.clock() - started) * 1000,
                tokens_in,
                tokens_out,
                first_input,
                tuple(raw),
            )
        # Fallback: the user's words are kept verbatim, marked unedited by the session.
        return TurnOutcome(
            "fallback",
            FALLBACK_REPLY,
            JournalOp("append", context.transcript.strip(), None, None),
            None,
            len(codes),
            tuple(codes),
            (self.clock() - started) * 1000,
            tokens_in,
            tokens_out,
            first_input,
            tuple(raw),
        )
