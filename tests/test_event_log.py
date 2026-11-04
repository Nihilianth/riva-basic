import io
import json

import pytest

from riva.event_log import Event, EventName, write_event


def test_event_is_one_json_line_without_free_text_fields() -> None:
    output = io.StringIO()
    write_event(
        Event(EventName.ASR_FINALIZED, "synthetic-session", "turn-1", duration_ms=12), output
    )
    record = json.loads(output.getvalue())
    assert len(output.getvalue().splitlines()) == 1
    assert record["name"] == "asr.finalized"
    assert record["duration_ms"] == 12
    assert set(record) == {
        "timestamp",
        "name",
        "session_id",
        "turn_id",
        "generation_id",
        "duration_ms",
    }


def test_negative_duration_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        Event(EventName.PROVIDER_FAILED, "synthetic-session", duration_ms=-1)


@pytest.mark.parametrize("duration", [float("nan"), float("inf")])
def test_nonfinite_duration_cannot_produce_invalid_json(duration: float) -> None:
    with pytest.raises(ValueError):
        write_event(
            Event(EventName.PROVIDER_FAILED, "synthetic-session", duration_ms=duration),
            io.StringIO(),
        )


def test_detail_is_a_short_token_and_only_present_when_set() -> None:
    output = io.StringIO()
    write_event(
        Event(EventName.BROWSER_HLS_ERROR, "browser", detail="levelEmptyError:fatal"), output
    )
    assert json.loads(output.getvalue())["detail"] == "levelEmptyError:fatal"
    for bad in ("two words", "a" * 49, "", "text\nwith newline"):
        with pytest.raises(ValueError, match="identifier token"):
            Event(EventName.BROWSER_AUDIO, "browser", detail=bad)
