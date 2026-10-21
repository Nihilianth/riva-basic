"""Stamp Gradio audio deliveries before its streaming queue can replay them."""

import json
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

DELIVERY_FIELD = "riva_audio_delivery_id"


class AudioDeliveryMiddleware:
    """Distinguish HTTP deliveries from repeated execution of one queued body.

    Gradio's close signal can wake a stream callback with its unchanged last body.
    A token stamped on HTTP arrival survives that replay, while a genuinely new
    delivery gets a fresh token even when its samples are byte-for-byte identical.
    This is not a capture sequence number or deduplication of network retries.
    """

    def __init__(self, app: ASGIApp, *, fn_index: int) -> None:
        self.app = app
        self.fn_index = fn_index

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        target = path == "/gradio_api/queue/join" or (
            path.startswith("/gradio_api/stream/") and not path.endswith("/close")
        )
        if scope["type"] != "http" or scope.get("method") != "POST" or not target:
            await self.app(scope, receive, send)
            return

        # These are small JSON references to files already uploaded by Gradio,
        # not audio uploads themselves. Don't buffer arbitrary large bodies.
        messages: list[Message] = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            size += len(message.get("body", b""))
            if message["type"] != "http.request" or not message.get("more_body") or size > 65536:
                break
        complete = messages[-1]["type"] == "http.request" and not messages[-1].get("more_body")
        if size > 65536:
            await send({"type": "http.response.start", "status": 413, "headers": []})
            await send({"type": "http.response.body", "body": b"Stream metadata too large"})
            return
        if complete and size <= 65536:
            body = b"".join(m.get("body", b"") for m in messages)
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict) and payload.get("fn_index") == self.fn_index:
                data = payload.get("event_data")
                data = dict(data) if isinstance(data, dict) else {}
                # Never trust a client-supplied token. This marks one received
                # request, not its content, identity, authorization or timestamp.
                data[DELIVERY_FIELD] = uuid4().hex
                payload["event_data"] = data
                stamped = json.dumps(payload).encode("utf-8")
                messages = [{"type": "http.request", "body": stamped, "more_body": False}]
                scope = dict(scope)
                headers = [
                    (k, v) for k, v in scope.get("headers", []) if k.lower() != b"content-length"
                ]
                scope["headers"] = [*headers, (b"content-length", str(len(stamped)).encode())]

        buffered = iter(messages)

        async def replay() -> Message:
            try:
                return next(buffered)
            except StopIteration:
                return await receive()

        await self.app(scope, replay, send)
