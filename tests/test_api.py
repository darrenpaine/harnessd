"""POST /v1/agent/run (SPEC-0002), with the graph and guard injected."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any

import httpx
import pytest
import redis.exceptions
from langchain_core.exceptions import (
    ModelAuthenticationError,
    ModelNotFoundError,
    ModelRateLimitError,
)
from langchain_core.messages import AIMessage
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import InMemorySaver
from ollama import ResponseError as OllamaResponseError
from redisvl.exceptions import RedisSearchError

from agent.graph import build_graph
from agent.persistence import lock_key
from agent_support import Agent, note, tool_turn
from app import Services, app, get_graph, get_guard
from fakes import RaisingChatModel
from settings import Settings, get_settings
from tools import TOOLS
from tools.base import ToolContext

RUN = "/v1/agent/run"

MakeClient = Callable[..., tuple[httpx.AsyncClient, Agent]]


@pytest.fixture
async def make_client(
    tool_context: ToolContext,
) -> AsyncGenerator[MakeClient]:
    """Build a client whose app runs a scripted agent on an in-memory graph."""
    clients: list[httpx.AsyncClient] = []

    def make(
        replies: list[AIMessage], **agent_options: Any
    ) -> tuple[httpx.AsyncClient, Agent]:
        agent = Agent(tool_context, replies, **agent_options)
        app.dependency_overrides[get_settings] = lambda: tool_context.settings
        app.dependency_overrides[get_graph] = lambda: agent.graph
        app.dependency_overrides[get_guard] = lambda: agent.guard
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        clients.append(client)
        return client, agent

    yield make
    for client in clients:
        await client.aclose()
    app.dependency_overrides.clear()


@pytest.mark.spec("SPEC-0002", "B-0002")
async def test_run_returns_agent_response(make_client: MakeClient) -> None:
    client, _ = make_client([AIMessage(content="hello there")])

    response = await client.post(RUN, json={"message": "hi"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["answer"] == "hello there"
    assert body["tool_calls"] == []


@pytest.mark.spec("SPEC-0002", "B-0001", "AT-0001")
@pytest.mark.parametrize("message", ["", "   "])
async def test_empty_message_returns_422(make_client: MakeClient, message: str) -> None:
    client, agent = make_client([])

    response = await client.post(RUN, json={"message": message})

    assert response.status_code == 422
    assert agent.model.calls == []


@pytest.mark.spec("SPEC-0002", "B-0001", "AT-0002")
@pytest.mark.parametrize("max_tool_calls", [0, 21])
async def test_max_tool_calls_bounds(
    make_client: MakeClient, max_tool_calls: int
) -> None:
    client, agent = make_client([])

    response = await client.post(
        RUN, json={"message": "hi", "max_tool_calls": max_tool_calls}
    )

    assert response.status_code == 422
    assert agent.model.calls == []


@pytest.mark.spec("SPEC-0002", "B-0001")
async def test_unknown_field_returns_422(make_client: MakeClient) -> None:
    client, _ = make_client([])
    response = await client.post(RUN, json={"message": "hi", "model": "gpt"})
    assert response.status_code == 422


@pytest.mark.spec("SPEC-0002", "B-0003", "AT-0004")
async def test_confirm_state_rules(make_client: MakeClient) -> None:
    client, agent = make_client([tool_turn(note("x"))])
    paused = (await client.post(RUN, json={"message": "note x"})).json()
    assert paused["status"] == "awaiting_confirmation"

    message = await client.post(
        RUN, json={"thread_id": paused["thread_id"], "message": "other"}
    )
    confirm = await client.post(RUN, json={"confirm": True})

    assert message.status_code == 422
    assert message.json()["detail"][0]["loc"] == ["body", "message"]
    assert confirm.status_code == 422
    assert confirm.json()["detail"][0]["loc"] == ["body", "confirm"]
    assert len(agent.model.calls) == 1


@pytest.mark.spec("SPEC-0002", "B-0004", "AT-0005")
async def test_concurrent_thread_returns_409(make_client: MakeClient) -> None:
    replies = [AIMessage(content="first"), AIMessage(content="second")]
    client, _ = make_client(replies, delay=0.2)
    thread_id = "6f1c2a9e-4b7d-4e2a-9c1f-3d5e8a7b2c10"
    body = {"thread_id": thread_id, "message": "hi"}

    first = asyncio.create_task(client.post(RUN, json=body))
    await asyncio.sleep(0.05)
    busy = await client.post(RUN, json=body)

    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "thread_busy"
    assert (await first).status_code == 200
    again = await client.post(RUN, json=body)
    assert again.status_code == 200
    assert again.json()["answer"] == "second"


@pytest.mark.spec("SPEC-0002", "B-0005", "AT-0006")
async def test_dependency_unavailable_returns_503(make_client: MakeClient) -> None:
    client, _ = make_client([])

    def redis_down() -> None:
        raise redis.exceptions.ConnectionError("Connection refused")

    app.dependency_overrides[get_graph] = redis_down
    response = await client.post(RUN, json={"message": "hi"})

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "dependency_unavailable", "message": "Redis is unreachable"}
    }


@pytest.mark.spec("SPEC-0002", "B-0005")
async def test_unreachable_redis_returns_503(make_client: MakeClient) -> None:
    client, _ = make_client([])
    settings = Settings(_env_file=None, redis_url="redis://127.0.0.1:1")  # type: ignore[call-arg]
    app.state.services = Services()
    app.dependency_overrides[get_settings] = lambda: settings
    del app.dependency_overrides[get_graph]

    response = await client.post(RUN, json={"message": "hi"})
    await app.state.services.aclose()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dependency_unavailable"
    assert "Traceback" not in response.text


@pytest.mark.spec("SPEC-0002", "B-0005")
async def test_unreachable_llm_returns_503(
    make_client: MakeClient, tool_context: ToolContext
) -> None:
    client, _ = make_client([])
    model = ChatOllama(model="llama3.1", base_url="http://127.0.0.1:1")
    graph = build_graph(model, tool_context, TOOLS, InMemorySaver())
    app.dependency_overrides[get_graph] = lambda: graph

    response = await client.post(RUN, json={"message": "hi"})

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "dependency_unavailable",
            "message": "the LLM provider is unreachable",
        }
    }


@pytest.mark.spec("SPEC-0002", "B-0006")
async def test_health_needs_no_dependencies(make_client: MakeClient) -> None:
    client, _ = make_client([])

    def fail() -> None:
        raise AssertionError("health must not build the graph")

    app.dependency_overrides[get_graph] = fail
    response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.spec("SPEC-0002", "B-0002")
async def test_thread_id_round_trip(make_client: MakeClient) -> None:
    client, _ = make_client([AIMessage(content="a"), AIMessage(content="b")])
    first = (await client.post(RUN, json={"message": "one"})).json()

    second = await client.post(
        RUN, json={"thread_id": first["thread_id"], "message": "two"}
    )

    assert second.json()["thread_id"] == first["thread_id"]


@pytest.mark.spec("SPEC-0002", "B-0005")
async def test_other_search_errors_are_not_503(make_client: MakeClient) -> None:
    make_client([])  # installs the overrides; this test needs its own client

    def broken_index() -> None:
        raise RedisSearchError("unknown index")

    app.dependency_overrides[get_graph] = broken_index
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as raw:
        response = await raw.post(RUN, json={"message": "hi"})
    assert response.status_code == 500
    assert "unknown index" not in response.text


@pytest.mark.redis
@pytest.mark.spec("SPEC-0002", "B-0005")
async def test_real_wiring_reaches_the_model(redis_url: str) -> None:
    """No overrides: the lifespan, Redis checkpointer and Redis lock all run, and
    the request gets as far as the (unreachable) model."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, redis_url=redis_url, ollama_base_url="http://127.0.0.1:1"
    )
    app.dependency_overrides[get_settings] = lambda: settings
    thread_id = "0b6f0c56-0d0e-4f39-9a59-2f3c2d1f7e01"
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.post(
                    RUN, json={"thread_id": thread_id, "message": "hi"}
                )
                services: Services = app.state.services
                locked = await services.redis(settings).exists(lock_key(thread_id))
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "the LLM provider is unreachable"
    assert not locked


@pytest.mark.spec("SPEC-0002", "B-0007", "AT-0008")
@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (OllamaResponseError("model 'llama9' not found", 404), "has no model"),
        (ModelNotFoundError("model not found"), "has no model"),
        (ModelAuthenticationError("invalid x-api-key sk-secret"), "API key"),
        (OllamaResponseError("unauthorized", 401), "API key"),
        (ModelRateLimitError("slow down"), "rate-limiting"),
        (OllamaResponseError("model does not support tools", 400), "rejected"),
    ],
    ids=[
        "ollama-404",
        "not-found",
        "authentication",
        "ollama-401",
        "rate-limit",
        "ollama-400",
    ],
)
async def test_provider_rejection_returns_503(
    make_client: MakeClient, tool_context: ToolContext, error: Exception, cause: str
) -> None:
    client, _ = make_client([])
    model = RaisingChatModel(error=error)
    graph = build_graph(model, tool_context, TOOLS, InMemorySaver())
    app.dependency_overrides[get_graph] = lambda: graph

    response = await client.post(RUN, json={"message": "hi"})

    assert response.status_code == 503
    detail = response.json()["error"]
    assert detail["code"] == "dependency_unavailable"
    assert cause in detail["message"]
    assert repr(tool_context.settings.llm_model) in detail["message"]
    assert "sk-secret" not in response.text


@pytest.mark.spec("SPEC-0002", "B-0008", "AT-0009")
async def test_unexpected_error_returns_500(
    make_client: MakeClient, tool_context: ToolContext
) -> None:
    make_client([])  # installs the overrides; this test needs its own client
    model = RaisingChatModel(error=RuntimeError("secret detail"))
    graph = build_graph(model, tool_context, TOOLS, InMemorySaver())
    app.dependency_overrides[get_graph] = lambda: graph
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as raw:
        response = await raw.post(RUN, json={"message": "hi"})

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal", "message": "internal error"}
    }
    assert "secret detail" not in response.text


@pytest.mark.spec("SPEC-0002", "B-0005")
async def test_shutdown_survives_redis_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Redis gone at shutdown: both closes are tried, nothing is raised."""
    services = Services()
    settings = Settings(_env_file=None, redis_url="redis://127.0.0.1:1")  # type: ignore[call-arg]
    client = services.redis(settings)
    closed: list[str] = []

    async def checkpointer_lost() -> None:
        closed.append("checkpointer")
        raise redis.exceptions.ConnectionError("Connection refused")

    async def client_timed_out() -> None:
        closed.append("client")
        raise redis.exceptions.TimeoutError("Timeout")

    services._stack.push_async_callback(checkpointer_lost)
    monkeypatch.setattr(client, "aclose", client_timed_out)

    await services.aclose()

    assert closed == ["checkpointer", "client"]
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 2
