import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from riva.experiments.gradio_delivery import DELIVERY_FIELD, AudioDeliveryMiddleware


def echo_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AudioDeliveryMiddleware, fn_index=7)

    @app.post("/{path:path}")
    async def echo(request: Request) -> dict:
        body = await request.body()
        assert len(body) == int(request.headers["content-length"])
        return json.loads(body)

    return app


def test_each_http_delivery_gets_a_token_not_an_audio_fingerprint() -> None:
    body = {"fn_index": 7, "data": ["synthetic-identical-audio"], "event_data": {"original": True}}
    with TestClient(echo_app()) as client:
        first = client.post("/gradio_api/queue/join", json=body).json()
        second = client.post("/gradio_api/stream/synthetic", json=body).json()
    assert first["event_data"][DELIVERY_FIELD] != second["event_data"][DELIVERY_FIELD]
    assert first["data"] == second["data"] == body["data"]
    assert first["event_data"]["original"] is True


def test_client_cannot_select_delivery_identity() -> None:
    body = {"fn_index": 7, "event_data": {DELIVERY_FIELD: "client-selected"}}
    with TestClient(echo_app()) as client:
        response = client.post("/gradio_api/queue/join", json=body).json()
    assert response["event_data"][DELIVERY_FIELD] != "client-selected"


def test_other_callbacks_and_close_messages_are_unchanged() -> None:
    unrelated = {"fn_index": 8, "data": ["synthetic"]}
    close = {"fn_index": 7}
    with TestClient(echo_app()) as client:
        assert client.post("/gradio_api/queue/join", json=unrelated).json() == unrelated
        assert client.post("/gradio_api/stream/synthetic/close", json=close).json() == close


def test_oversized_metadata_is_rejected_without_accepting_a_client_token() -> None:
    with TestClient(echo_app()) as client:
        response = client.post("/gradio_api/queue/join", json={"fn_index": 7, "data": "x" * 70000})
    assert response.status_code == 413


def test_gradio_close_wakeup_preserves_last_stamped_body(monkeypatch) -> None:
    # Exercise the installed queue's actual close-signal behavior, without
    # microphones, provider calls, or comparison of waveform/text content.
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    from gradio.queueing import Queue

    async def closed_stream():
        body = SimpleNamespace(event_data={DELIVERY_FIELD: "a" * 32})
        event = SimpleNamespace(signal=asyncio.Event(), closed=True, data=body)
        event.signal.set()
        awake, expired = await Queue.wait_for_batch([event], [1])
        assert awake == [event]
        assert expired == []
        assert awake[0].data is body
        assert awake[0].data.event_data[DELIVERY_FIELD] == "a" * 32

    asyncio.run(closed_stream())
