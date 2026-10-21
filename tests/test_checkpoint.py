"""Redis checkpointing, TTL and the thread lock (SPEC-0004).

These use real Redis (SPEC-0005 B-0003). Checkpointer tests use a minimal
one-node graph; the confirmation test runs the real agent.
"""

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from redis.asyncio import Redis

from agent.persistence import (
    LOCK_MARGIN_SECONDS,
    RedisThreadGuard,
    ThreadBusyError,
    lock_key,
    open_checkpointer,
    refresh_thread_ttl,
    thread_lock,
    ttl_seconds,
)
from agent_support import Agent, MakeClient, note, tool_turn
from fakes import ScriptedChatModel
from settings import Settings
from tools.base import ToolContext

pytestmark = pytest.mark.redis

# Shown when AT-0006 fails: the likely cause is a library upgrade.
PRIVATE_API_HINT = (
    "repeated confirmations broke: agent.persistence.ShallowRedisSaver overrides "
    "the private AsyncShallowRedisSaver._aload_pending_writes of "
    "langgraph-checkpoint-redis; if the package was upgraded, check that the "
    "override still applies (SPEC-0004 B-0006)"
)


@pytest.fixture
def settings(redis_url: str) -> Settings:
    return Settings(_env_file=None, redis_url=redis_url)  # type: ignore[call-arg]


@pytest.fixture
async def redis(redis_url: str) -> AsyncGenerator[Redis]:
    client = Redis.from_url(redis_url)
    yield client
    await client.aclose()


def chat_graph(model: ScriptedChatModel, saver: BaseCheckpointSaver[Any]) -> Any:
    async def call_model(state: MessagesState) -> dict[str, list[BaseMessage]]:
        return {"messages": [await model.ainvoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("model", call_model)
    graph.add_edge(START, "model")
    graph.add_edge("model", END)
    return graph.compile(checkpointer=saver)


def config(thread_id: str) -> Any:
    return {"configurable": {"thread_id": thread_id}}


async def thread_ttls(redis: Redis, thread_id: str) -> list[int]:
    return [await redis.pttl(k) async for k in redis.scan_iter(match=f"*{thread_id}*")]


@pytest.mark.spec("SPEC-0004", "B-0001", "AT-0001")
async def test_thread_resumes_from_redis(settings: Settings) -> None:
    thread_id = str(uuid.uuid4())
    model = ScriptedChatModel(
        replies=[AIMessage(content="first"), AIMessage(content="second")]
    )
    async with open_checkpointer(settings) as saver:
        graph = chat_graph(model, saver)
        await graph.ainvoke({"messages": [("user", "hello")]}, config(thread_id))
    async with open_checkpointer(settings) as saver:
        graph = chat_graph(model, saver)
        await graph.ainvoke({"messages": [("user", "again")]}, config(thread_id))

    assert [m.content for m in model.calls[1]] == ["hello", "first", "again"]


@pytest.mark.spec("SPEC-0004", "B-0002", "AT-0002")
async def test_unknown_thread_starts_fresh(settings: Settings) -> None:
    model = ScriptedChatModel(replies=[AIMessage(content="hi")])
    async with open_checkpointer(settings) as saver:
        graph = chat_graph(model, saver)
        await graph.ainvoke({"messages": [("user", "new")]}, config(str(uuid.uuid4())))

    assert [m.content for m in model.calls[0]] == ["new"]


@pytest.mark.spec("SPEC-0004", "B-0003", "AT-0003")
async def test_checkpoint_ttl_set(settings: Settings, redis: Redis) -> None:
    thread_id = str(uuid.uuid4())
    limit_ms = ttl_seconds(settings) * 1000
    model = ScriptedChatModel(replies=[AIMessage(content="a"), AIMessage(content="b")])
    async with open_checkpointer(settings) as saver:
        graph = chat_graph(model, saver)
        await graph.ainvoke({"messages": [("user", "one")]}, config(thread_id))
        await refresh_thread_ttl(redis, thread_id, ttl_seconds(settings))
        first = await thread_ttls(redis, thread_id)

        await asyncio.sleep(1.5)
        await graph.ainvoke({"messages": [("user", "two")]}, config(thread_id))
        updated = await refresh_thread_ttl(redis, thread_id, ttl_seconds(settings))
        second = await thread_ttls(redis, thread_id)

    assert first and all(0 < t <= limit_ms for t in first)
    assert updated == len(second) > len(first)
    # Every key, including those from the first run, was reset by the second.
    assert min(second) > limit_ms - 1000


@pytest.mark.spec("SPEC-0004", "B-0004", "AT-0004")
async def test_pending_confirmation_survives_restart(
    settings: Settings, redis: Redis, tool_context: ToolContext
) -> None:
    agent_settings = tool_context.settings.model_copy(
        update={"redis_url": settings.redis_url}
    )
    ctx = dataclasses.replace(tool_context, settings=agent_settings)
    guard = RedisThreadGuard(redis, ctx.settings)
    async with open_checkpointer(ctx.settings) as saver:
        agent = Agent(ctx, [tool_turn(note("kept"))], checkpointer=saver, guard=guard)
        paused = await agent.run(message="note: kept")
    assert paused.status == "awaiting_confirmation"
    assert await thread_ttls(redis, str(paused.thread_id))

    # A new checkpointer and graph, as after an app restart.
    async with open_checkpointer(ctx.settings) as saver:
        replies = [AIMessage(content="noted")]
        agent = Agent(ctx, replies, checkpointer=saver, guard=guard)
        done = await agent.run(thread_id=paused.thread_id, confirm=True)

    assert done.status == "ok"
    assert [r.name for r in done.tool_calls] == ["add_note"]
    assert ctx.settings.notes_path.read_text().endswith("\tkept\n")
    assert not await redis.exists(lock_key(str(paused.thread_id)))


@pytest.mark.spec("SPEC-0004", "B-0005", "AT-0005")
async def test_thread_lock_released(settings: Settings, redis: Redis) -> None:
    thread_id = str(uuid.uuid4())
    key = lock_key(thread_id)

    async with thread_lock(redis, thread_id, settings):
        expiry_ms = await redis.pttl(key)
        assert (
            0 < expiry_ms <= (settings.run_timeout_seconds + LOCK_MARGIN_SECONDS) * 1000
        )
        with pytest.raises(ThreadBusyError):
            async with thread_lock(redis, thread_id, settings):
                pass
    assert not await redis.exists(key)

    with pytest.raises(RuntimeError):
        async with thread_lock(redis, thread_id, settings):
            raise RuntimeError("run failed")
    assert not await redis.exists(key)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05), thread_lock(redis, thread_id, settings):
            await asyncio.sleep(1)
    assert not await redis.exists(key)


@pytest.mark.spec("SPEC-0004", "B-0005")
async def test_thread_lock_tolerates_expired_lock(
    settings: Settings, redis: Redis
) -> None:
    thread_id = str(uuid.uuid4())
    async with thread_lock(redis, thread_id, settings):
        await redis.delete(lock_key(thread_id))  # as if it expired mid-run
    assert not await redis.exists(lock_key(thread_id))


@pytest.mark.spec("SPEC-0004", "B-0006", "AT-0006")
async def test_repeated_confirmations(
    settings: Settings,
    redis: Redis,
    tool_context: ToolContext,
    make_client: MakeClient,
) -> None:
    agent_settings = tool_context.settings.model_copy(
        update={"redis_url": settings.redis_url}
    )
    guard = RedisThreadGuard(redis, agent_settings)
    replies: list[AIMessage] = []
    for text in ("one", "two", "three"):
        replies += [tool_turn(note(text)), AIMessage(content=f"done {text}")]
    thread_id = str(uuid.uuid4())

    async def send(client: httpx.AsyncClient, body: dict[str, Any]) -> Any:
        response = await client.post(
            "/v1/agent/run", json={"thread_id": thread_id, **body}
        )
        state = await client.get(f"/v1/threads/{thread_id}")
        assert response.status_code == 200, f"{PRIVATE_API_HINT}\n{response.text}"
        assert state.status_code == 200, f"{PRIVATE_API_HINT}\n{state.text}"
        return response.json()

    async with open_checkpointer(agent_settings) as saver:
        client, _ = make_client(replies, checkpointer=saver, guard=guard)
        results = []
        for text, approve in (("one", True), ("two", True), ("three", False)):
            paused = await send(client, {"message": f"note: {text}"})
            done = await send(client, {"confirm": approve})
            results.append((paused["status"], done["status"]))

    expected = [("awaiting_confirmation", "ok")] * 3
    assert results == expected, PRIVATE_API_HINT
    [rejected] = done["tool_calls"]
    assert rejected["error"]["code"] == "rejected", PRIVATE_API_HINT
    lines = agent_settings.notes_path.read_text().splitlines()
    assert [line.split("\t")[-1] for line in lines] == ["one", "two"]
