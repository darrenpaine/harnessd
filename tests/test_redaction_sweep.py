"""No secret leaves the API on any route (SPEC-0012 B-0003).

One conversation puts a configured secret everywhere model or tool text can
appear, then calls every route the app has. The test fails when the secret is in
a response body outside the fields B-0003 leaves raw, and when the app has a
route this test does not call, so a new route cannot be added without it.
"""

import dataclasses
import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from agent.redact import REDACTED
from agent_support import MakeClient, fetch, tool_turn
from app import app
from tools.base import ToolContext

SECRET = "tvly-test-0123456789"  # gitleaks:allow (made-up key)
RUN = "/v1/agent/run"
THREAD = "/v1/threads/{thread_id}"
COMPACT = "/v1/threads/{thread_id}/compact"
HEALTH = "/health"


def app_routes() -> set[tuple[str, str]]:
    """Every (method, path) in the app's OpenAPI schema, so the docs pages are
    left out."""
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }


def without_raw_fields(body: Any) -> Any:
    """The body without what B-0003 leaves raw: tool call records. Pending
    actions and the thread view's tool-call arguments are also raw today; the
    spec doesn't name them."""
    if isinstance(body, dict):
        return {
            key: without_raw_fields(value)
            for key, value in body.items()
            if key not in ("tool_calls", "pending_action")
        }
    if isinstance(body, list):
        return [without_raw_fields(item) for item in body]
    return body


def page_with_secret(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "text/plain"}, text=f"the key is {SECRET}"
    )


@pytest.mark.spec("SPEC-0012", "B-0003")
async def test_no_route_returns_a_secret(
    make_client: MakeClient, tool_context: ToolContext
) -> None:
    settings = tool_context.settings.model_copy(
        update={"tavily_api_key": SecretStr(SECRET)}
    )
    ctx = dataclasses.replace(tool_context, settings=settings)
    replies = [
        tool_turn(fetch(), text=f"reading, with {SECRET}"),
        AIMessage(content=f"the page says {SECRET}"),
        AIMessage(content=f"again: {SECRET}"),
        AIMessage(content=f"they asked for {SECRET}"),  # the summary
    ]
    client, _ = make_client(replies, ctx=ctx, serve=page_with_secret)
    called: set[tuple[str, str]] = set()

    async def call(
        method: str, route: str, *, carries_text: bool, **kwargs: Any
    ) -> httpx.Response:
        """Call `route` and check its body. `carries_text` says the response
        holds model or tool text, so a redaction must show in it."""
        response = await client.request(
            method, route.format(thread_id=thread_id), **kwargs
        )
        called.add((method, route))
        assert response.status_code < 300, response.text
        if not response.content:
            return response
        checked = json.dumps(without_raw_fields(response.json()))
        assert SECRET not in checked, f"{method} {route} returned a secret"
        if carries_text:
            assert REDACTED in checked, f"{method} {route}: nothing was redacted"
        return response

    thread_id = ""
    first = await call(
        "POST", RUN, carries_text=True, json={"message": f"read it; {SECRET}"}
    )
    thread_id = first.json()["thread_id"]
    again = {"thread_id": thread_id, "message": "and again"}
    await call("POST", RUN, carries_text=True, json=again)
    await call("GET", THREAD, carries_text=True)
    await call("POST", COMPACT, carries_text=True, json={"keep_turns": 1})
    await call("GET", THREAD, carries_text=True)  # now with the summary
    await call("GET", HEALTH, carries_text=False)
    await call("DELETE", THREAD, carries_text=False)

    assert called == app_routes(), "a route is not covered by this test"
