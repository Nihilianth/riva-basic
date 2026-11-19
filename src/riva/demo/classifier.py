"""Optional utterance classifier (meta / correction / diary) via TypeSafe Jev (riva-skm.33).

Off by default. When enabled, one typed multiple-choice question per turn labels the participant's
utterance so the turn prompt can route it (meta -> not journalled, correction -> revise, diary ->
append; see riva-skm.32). It is advisory: a low-confidence result or any error falls back to the
prompt-only behaviour, and the classifier never blocks or fails a turn.

The decision uses ``probabilities[choice]`` (the probability of the chosen label), per TypeSafe's
"classification using confidence" cookbook: for a choice question the ``confidence`` field is only a
transform of the top probability, so the probability of the option you act on is the signal to
threshold. Thresholds are domain-specific; start conservative and tune on real sessions.

Contract (direct gateway, confirmed live): POST ``<base>/v1/systemone`` with
``Authorization: Bearer $TYPESAFE_API_KEY``; body ``{model, state, questions:{kind:{type:"choice",
instructions, criteria:{label: description}}}}``; response ``answers.kind.{choice, confidence,
probabilities}``. The transcript (``state``) is the only content sent; nothing is logged here.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Protocol

LABELS = ("diary", "correction", "meta")

_INSTRUCTIONS = "Worum geht es in dieser Äußerung in einem Tagebuch-Gespräch?"
_CRITERIA = {
    "diary": "Die Person erzählt etwas über ihren Tag oder ein Erlebnis (Inhalt fürs Tagebuch).",
    "correction": "Die Person berichtigt einen früheren Tagebuch-Eintrag.",
    "meta": "Die Person spricht über die Software oder das Gespräch oder stellt dir eine Frage.",
}


@dataclass(frozen=True)
class Classification:
    label: str  # one of LABELS
    probability: float  # probability of the chosen label (0..1)
    latency_ms: float  # how long the classifier call took (for profiling)


class Classifier(Protocol):
    def classify(self, text: str) -> Classification | None:
        """Classify the utterance, or return None when unavailable/uncertain-to-parse/errored."""
        ...


class JevClassifier:
    """TypeSafe Jev classifier over the direct gateway. Errors/timeouts return None (fall back)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "jev-latest",
        timeout: float = 4.0,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY", "")
        base = base_url if base_url is not None else os.environ.get("TYPESAFE_BASE_URL", "")
        self.api_key = key.strip()
        self.base_url = (base or "https://api.typesafe.ai").rstrip("/")
        self.model = model
        self.timeout = timeout

    def classify(self, text: str) -> Classification | None:
        if not self.api_key or not text.strip():
            return None
        body = json.dumps(
            {
                "model": self.model,
                "state": text,
                "questions": {
                    "kind": {"type": "choice", "instructions": _INSTRUCTIONS, "criteria": _CRITERIA}
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - fixed https gateway from config/env
            f"{self.base_url}/v1/systemone",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        start = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                data = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - any failure is advisory: fall back to prompt-only
            return None
        latency_ms = (time.monotonic() - start) * 1000
        answer = (data.get("answers") or {}).get("kind") or {}
        label = answer.get("choice")
        if label not in LABELS:
            return None
        probabilities = answer.get("probabilities") or {}
        raw = probabilities.get(label, answer.get("confidence"))
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            return None
        return Classification(label=label, probability=float(raw), latency_ms=latency_ms)
