"""PAID, opt-in: a small bounded LLM smoke test on synthetic German examples.

Run only with the user's approval:

    RIVA_RUN_LIVE_TESTS=1 uv run --env-file .env pytest -m live tests/demo/live -s

Budget is bounded by construction: at most ``MAX_CALLS`` requests, each limited to 700 output
tokens, and an abort once the estimated cost passes ``MAX_COST_USD`` (prices are approximate).
Results (synthetic content only) go to the private archive, never to Git or the event log.
"""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support.conversations import load_all

from riva.audio.recordings import private_directory
from riva.demo.config import load_config
from riva.demo.openai_llm import MAX_OUTPUT_TOKENS, OpenAITurnLLM
from riva.demo.prompts import TurnContext
from riva.demo.turn import LLMReply, TurnProcessor

pytestmark = pytest.mark.live

MAX_CALLS = 14
MAX_COST_USD = 0.25
# USD per 1M tokens (input, output); approximate - the cap below also bounds tokens, not just price.
PRICES = {"gpt-4.1-mini": (0.40, 1.60), "gpt-4.1": (2.00, 8.00)}


class BudgetStop(BaseException):
    """Not an Exception on purpose: the turn processor must not swallow it as a provider error."""


class BudgetedLLM:
    def __init__(self) -> None:
        self.inner = OpenAITurnLLM()
        self.calls = 0
        self.cost = 0.0

    def respond(
        self, instructions: str, user_input: str, *, model: str, timeout: float
    ) -> LLMReply:
        if self.calls >= MAX_CALLS or self.cost >= MAX_COST_USD:
            raise BudgetStop("live budget exhausted")
        self.calls += 1
        reply = self.inner.respond(instructions, user_input, model=model, timeout=timeout)
        price_in, price_out = PRICES[model]
        self.cost += (reply.input_tokens * price_in + reply.output_tokens * price_out) / 1e6
        return reply


def context_for(scenario: str, turn_index: int) -> tuple[TurnContext, dict, frozenset[str]]:
    turns = next(s for s in load_all() if s.name == scenario).turns
    prior = turns[:turn_index]
    journal = tuple((f"p{i}", t["intended"]) for i, t in enumerate(prior))
    history = tuple(
        (
            " ".join(s["asr_text"] for s in t["segments"]),
            "Das klingt schön. Magst du mehr erzählen?",
        )
        for t in prior
    )
    turn = turns[turn_index]
    transcript = " ".join(s["asr_text"] for s in turn["segments"])
    ctx = TurnContext(transcript=transcript, journal_tail=journal, history=history)
    return ctx, turn, frozenset(pid for pid, _ in journal)


CASES = [
    ("zoo-basic", 1, "gpt-4.1-mini"),
    ("self-correction-revise", 1, "gpt-4.1-mini"),
    ("ambiguous-correction", 1, "gpt-4.1-mini"),
    ("asr-homophone-error", 0, "gpt-4.1-mini"),
    ("repetition-stutter", 0, "gpt-4.1-mini"),
    ("self-correction-revise", 1, "gpt-4.1"),
    ("ambiguous-correction", 1, "gpt-4.1"),
]


def test_bounded_llm_smoke_on_synthetic_german_examples() -> None:
    assert os.environ.get("OPENAI_API_KEY"), "run with --env-file .env"
    config, llm = load_config(), BudgetedLLM()
    processor = TurnProcessor(llm, config)
    results = []
    for scenario, index, model in CASES:
        ctx, turn, revisable = context_for(scenario, index)
        started = time.monotonic()
        outcome = processor.process(ctx, persona_id="buddy", model=model, revisable_ids=revisable)
        expect = turn["expect"]
        results.append(
            {
                "scenario": scenario,
                "turn": index,
                "model": model,
                "source": outcome.source,
                "attempts": outcome.attempts,
                "error_codes": outcome.error_codes,
                "latency_ms": round(outcome.latency_ms),
                "wall_ms": round((time.monotonic() - started) * 1000),
                "input_tokens": outcome.input_tokens,
                "output_tokens": outcome.output_tokens,
                "expected_op": expect["journal_op"],
                "journal_op": outcome.journal_op.type,
                "op_matches": outcome.journal_op.type == expect["journal_op"],
                "uncertain": outcome.uncertain,
                "soft_flags": list(outcome.validated.soft_flags) if outcome.validated else [],
                "grounding": outcome.validated.grounding if outcome.validated else None,
                "flags": list(outcome.flags),
                "transcript": ctx.transcript,
                "intended": turn["intended"],
                "passage": outcome.journal_op.text,
                "spoken_reply": outcome.spoken_reply,
            }
        )
    root = private_directory(Path(os.environ.get("RIVA_PRIVATE_DIR", "../private")))
    target = private_directory(root / "research-data" / "demo-1" / "live-eval")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    summary = {"calls": llm.calls, "estimated_cost_usd": round(llm.cost, 4), "results": results}
    (target / f"llm-smoke-{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\ncalls={llm.calls} est_cost=${llm.cost:.4f} max_output_tokens={MAX_OUTPUT_TOKENS}")
    for row in results:
        print(
            f"{row['model']:13s} {row['scenario']:24s} src={row['source']:8s} "
            f"att={row['attempts']} "
            f"{row['latency_ms']:5d} ms op={row['journal_op']}(exp {row['expected_op']}) "
            f"unc={row['uncertain']} ground={row['grounding']}"
        )
    # Schema compliance is the hard requirement; meaning is judged from the saved results.
    assert all(row["source"] == "llm" for row in results)
