"""OpenAI adapter for ``TurnLLM`` (Responses API, strict structured output, nothing stored)."""

from riva.demo.schema import turn_schema
from riva.demo.turn import LLMReply

MAX_OUTPUT_TOKENS = 700


class OpenAITurnLLM:
    """Paid. Only constructed by the real app or by explicitly approved live tests."""

    def respond(
        self, instructions: str, user_input: str, *, model: str, timeout: float
    ) -> LLMReply:
        from openai import OpenAI

        with OpenAI(max_retries=0, timeout=timeout) as client:
            response = client.responses.create(
                model=model,
                instructions=instructions,
                input=user_input,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                store=False,
                temperature=0.4,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "turn_result",
                        "schema": turn_schema(),
                        "strict": True,
                    }
                },
            )
        if response.status != "completed":
            raise RuntimeError("incomplete response")
        usage = response.usage
        return LLMReply(
            response.output_text,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
        )
