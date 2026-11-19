"""Optional Jev classifier wiring into the turn (riva-skm.33): hint, threshold, profiling, fallback.

The network client (JevClassifier) is exercised only by the gated live test; here a scripted
classifier drives the session path.
"""

import json

from support.demo import ScriptedClassifier, make_harness

from riva.demo.classifier import JevClassifier


def _llm_input(h, report):
    path = h.archive.directory / "utterances" / f"{report.utterance_id}.json"
    return json.loads(path.read_text())["llm_input"]


def _events(h):
    return [e for e in h.events() if e["event"] == "classified"]


def test_confident_label_hints_the_prompt_and_is_profiled(tmp_path) -> None:
    clf = ScriptedClassifier(label="meta", probability=0.97, latency_ms=215.0)
    h = make_harness(tmp_path, classifier=clf, classifier_enabled=True)
    report = h.answer("Warum fragst du mich das?")
    assert report is not None and clf.calls == 1
    assert "automatische Einstufung" in _llm_input(h, report)  # the hint reached the prompt
    (event,) = _events(h)
    assert event["label"] == "meta" and event["used"] is True
    assert event["latency_ms"] == 215  # Jev latency is recorded per turn (profiling)


def test_low_confidence_is_recorded_but_not_used(tmp_path) -> None:
    clf = ScriptedClassifier(label="correction", probability=0.61)  # below the 0.75 threshold
    h = make_harness(tmp_path, classifier=clf, classifier_enabled=True)
    report = h.answer("Nein, es waren vier.")
    assert report is not None
    assert "automatische Einstufung" not in _llm_input(h, report)  # not hinted below threshold
    (event,) = _events(h)
    assert event["used"] is False  # still recorded for review/profiling


def test_disabled_classifier_is_never_called(tmp_path) -> None:
    clf = ScriptedClassifier(label="meta", probability=1.0)
    h = make_harness(tmp_path, classifier=clf, classifier_enabled=False)
    report = h.answer("Hallo")
    assert report is not None and clf.calls == 0
    assert "automatische Einstufung" not in _llm_input(h, report)
    assert _events(h) == []


def test_classifier_error_falls_back_without_crashing(tmp_path) -> None:
    clf = ScriptedClassifier(label=None)  # unavailable / unparseable -> classify returns None
    h = make_harness(tmp_path, classifier=clf, classifier_enabled=True)
    report = h.answer("Heute war ich im Zoo.")
    assert report is not None  # the turn still completes
    assert "automatische Einstufung" not in _llm_input(h, report)
    assert _events(h) == []


def test_jev_classifier_without_a_key_returns_none(tmp_path) -> None:
    assert JevClassifier(api_key="").classify("Hallo") is None  # no key -> no call, safe fallback
