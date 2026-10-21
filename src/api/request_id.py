"""X-Request-ID on every response and log line (SPEC-0010 B-0006, B-0007)."""

import re
import uuid

from opentelemetry import trace
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from logs import log_context

HEADER = "X-Request-ID"
VALID = re.compile(r"[A-Za-z0-9._-]{1,128}")


def request_id(supplied: str | None) -> str:
    """The client's id if it is 1-128 of `[A-Za-z0-9._-]`, otherwise a new UUID4.
    A bad id is replaced, never rejected (B-0007)."""
    if supplied is not None and VALID.fullmatch(supplied):
        return supplied
    return str(uuid.uuid4())


class RequestIdMiddleware:
    """Tag the request's log lines and server span with its id, and return the
    id as a header.

    Added after the error handling, so it wraps it: 422 and 500 responses, and
    the `request.failed` log line, carry the id too.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = next(
            (v.decode("latin-1") for k, v in scope["headers"] if k == b"x-request-id"),
            None,
        )
        rid = request_id(supplied)
        trace.get_current_span().set_attribute("request_id", rid)  # B-0006

        async def tagged(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[HEADER] = rid
            await send(message)

        with log_context(request=rid):
            await self.app(scope, receive, tagged)
