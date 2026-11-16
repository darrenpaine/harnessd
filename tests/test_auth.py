"""The shared API key and the unknown thread rule (SPEC-0013)."""

import dataclasses
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from agent_support import Agent, MakeClient
from api.deps import get_graph
from app import app
from tools.base import ToolContext

RUN = "/v1/agent/run"
KEY = "k" * 32
OPEN_PATHS = ("/health", "/docs", "/redoc", "/openapi.json")


def with_key(ctx: ToolContext) -> ToolContext:
    settings = ctx.settings.model_copy(update={"api_key": SecretStr(KEY)})
    return dataclasses.replace(ctx, settings=settings)


def count_graph_builds(agent: Agent) -> list[int]:
    """Record each time a route asks for the graph."""
    builds: list[int] = []

    def graph() -> object:
        builds.append(1)
        return agent.graph

    app.dependency_overrides[get_graph] = graph
    return builds


def error_code(response: httpx.Response) -> str:
    code: str = response.json()["error"]["code"]
    return code


@pytest.mark.spec("SPEC-0013", "B-0001", "B-0002", "AT-0001")
async def test_api_key(make_client: MakeClient, tool_context: ToolContext) -> None:
    client, agent = make_client(
        [AIMessage(content="hello")], ctx=with_key(tool_context)
    )
    builds = count_graph_builds(agent)
    thread = f"/v1/threads/{uuid4()}"

    refused = [
        await client.post(RUN, json={"message": "hi"}),
        await client.post(RUN, json={"message": "hi"}, headers={"X-API-Key": "no"}),
        await client.post(RUN, json={"message": "hi"}, headers={"X-API-Key": ""}),
        await client.post(RUN, json={}),  # refused before the body is checked
        await client.get(thread),
        await client.delete(thread),
        await client.post(f"{thread}/compact"),
    ]
    for response in refused:
        assert response.status_code == 401
        assert error_code(response) == "unauthorized"
        assert KEY not in response.text
    assert builds == []
    assert agent.model.calls == []

    allowed = await client.post(RUN, json={"message": "hi"}, headers={"X-API-Key": KEY})
    assert allowed.status_code == 200
    assert allowed.json()["answer"] == "hello"
    for path in OPEN_PATHS:
        assert (await client.get(path)).status_code == 200

    spec = (await client.get("/openapi.json")).json()
    schemes = spec["components"]["securitySchemes"]
    assert {"type": "apiKey", "in": "header", "name": "X-API-Key"}.items() <= next(
        iter(schemes.values())
    ).items()
    assert spec["paths"][RUN]["post"]["security"]
    assert "security" not in spec["paths"]["/health"]["get"]

    # B-0002: with no key set, nothing changes.
    client, _ = make_client([AIMessage(content="hello")])
    assert (await client.post(RUN, json={"message": "hi"})).status_code == 200


@pytest.mark.spec("SPEC-0013", "B-0003", "AT-0002")
async def test_unknown_thread_refused(
    make_client: MakeClient, tool_context: ToolContext
) -> None:
    client, agent = make_client(
        [AIMessage(content="first"), AIMessage(content="second")],
        ctx=with_key(tool_context),
    )
    client.headers["X-API-Key"] = KEY

    for body in ({"message": "hi"}, {"confirm": True}):
        unknown = await client.post(RUN, json={"thread_id": str(uuid4()), **body})
        assert unknown.status_code == 404
        assert error_code(unknown) == "thread_not_found"
    assert agent.model.calls == []

    started = await client.post(RUN, json={"thread_id": None, "message": "hi"})
    assert started.status_code == 200
    thread_id = started.json()["thread_id"]
    assert UUID(thread_id).version == 4
    continued = await client.post(RUN, json={"thread_id": thread_id, "message": "more"})
    assert continued.status_code == 200
    assert continued.json()["answer"] == "second"

    # With no key set, an unknown id starts a thread with that id (SPEC-0004 B-0002).
    client, _ = make_client([AIMessage(content="hello")])
    chosen = str(uuid4())
    response = await client.post(RUN, json={"thread_id": chosen, "message": "hi"})
    assert response.status_code == 200
    assert response.json()["thread_id"] == chosen
