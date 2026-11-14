"""Redis connections that drop or go stale, retries and rebuilds (SPEC-0014).

Tests marked `redis` use a real Redis (SPEC-0005 B-0003); the retry tests use
fakes, as a real Redis cannot be made to fail on cue.
"""

import asyncio
import dataclasses
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import redis.exceptions
from langchain_core.messages import AIMessage
from redis.asyncio import BlockingConnectionPool, Redis
from redisvl.exceptions import RedisSearchError

import agent.persistence
from agent.persistence import (
    CLIENT_NAME,
    RedisThreadGuard,
    build_redis,
    open_checkpointer,
    retrying,
)
from agent_support import Agent, MakeClient
from api.deps import Services, get_guard
from app import app
from settings import Settings
from tools.base import ToolContext

RUN = "/v1/agent/run"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent.persistence, "BACKOFF_BASE", 0.0)
    monkeypatch.setattr(agent.persistence, "BACKOFF_CAP", 0.0)


def redis_settings(
    tool_context: ToolContext, redis_url: str, **values: Any
) -> Settings:
    return tool_context.settings.model_copy(update={"redis_url": redis_url, **values})


@pytest.fixture
async def admin(redis_url: str) -> AsyncGenerator[Redis]:
    """A separate client, for killing the app's connections."""
    client = Redis.from_url(redis_url)
    yield client
    await client.aclose()


async def kill_our_connections(admin: Redis) -> int:
    clients = await admin.client_list()
    ours = [c["id"] for c in clients if c.get("name") == CLIENT_NAME]
    for client_id in ours:
        await admin.client_kill_filter(_id=client_id)
    return len(ours)


@pytest.mark.redis
@pytest.mark.spec("SPEC-0014", "B-0001", "B-0003", "AT-0001")
async def test_stale_connection_replaced(
    tool_context: ToolContext, redis_url: str, admin: Redis
) -> None:
    settings = redis_settings(tool_context, redis_url)
    ctx = dataclasses.replace(tool_context, settings=settings)
    client = build_redis(settings)
    guard = RedisThreadGuard(client, settings)
    try:
        async with open_checkpointer(settings, client) as saver:
            first = await Agent(
                ctx, [AIMessage(content="one")], checkpointer=saver, guard=guard
            ).run(message="hi")
            assert await kill_our_connections(admin) > 0
            await asyncio.sleep(0.1)  # between requests: the close reaches us

            second = await Agent(
                ctx, [AIMessage(content="two")], checkpointer=saver, guard=guard
            ).run(message="again", thread_id=first.thread_id)
    finally:
        await client.aclose()

    assert second.status == "ok"
    assert second.answer == "two"


@pytest.mark.redis
@pytest.mark.spec("SPEC-0014", "B-0003")
async def test_rebuild_after_failure(
    tool_context: ToolContext, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a Redis failure the next request gets a new graph, but only once per
    REDIS_HEALTH_CHECK_SECONDS."""
    settings = redis_settings(tool_context, redis_url, redis_health_check_seconds=30)
    clock = [1000.0]
    monkeypatch.setattr("api.deps.time.monotonic", lambda: clock[0])
    services = Services()
    try:
        first = await services.graph(settings)
        assert await services.graph(settings) is first

        clock[0] += 31
        services.redis_failed()
        rebuilt = await services.graph(settings)
        services.redis_failed()
        within_interval = await services.graph(settings)
        clock[0] += 31
        after_interval = await services.graph(settings)
    finally:
        await services.aclose()

    assert rebuilt is not first
    assert within_interval is rebuilt
    assert after_interval is not rebuilt


class Flaky:
    """An operation failing `failures` times with `error`, then answering."""

    def __init__(self, failures: int, error: Exception) -> None:
        self.failures = failures
        self.error = error
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return "read"


def wrapped_connection_error() -> RedisSearchError:
    try:
        raise redis.exceptions.ConnectionError("reset")
    except redis.exceptions.ConnectionError as exc:
        try:
            raise RedisSearchError("search failed") from exc
        except RedisSearchError as wrapped:
            return wrapped


class FailingLock:
    def __init__(self) -> None:
        self.acquires = 0

    async def acquire(self) -> bool:
        self.acquires += 1
        raise redis.exceptions.ConnectionError("reset")


class LockOnlyRedis:
    def __init__(self) -> None:
        self.held = FailingLock()

    def lock(self, name: str, **options: Any) -> FailingLock:
        return self.held


@pytest.mark.spec("SPEC-0014", "B-0002", "AT-0002")
@pytest.mark.parametrize(
    "error",
    [
        redis.exceptions.ConnectionError("reset"),
        redis.exceptions.TimeoutError("slow"),
        wrapped_connection_error(),
    ],
    ids=["connection", "timeout", "wrapped"],
)
async def test_retries(
    tool_context: ToolContext, make_client: MakeClient, error: Exception
) -> None:
    settings = tool_context.settings.model_copy(update={"redis_retries": 3})

    twice = Flaky(2, error)
    assert await retrying(settings, twice) == "read"
    assert twice.calls == 3

    always = Flaky(99, error)
    with pytest.raises(type(error)):
        await retrying(settings, always)
    assert always.calls == 4

    other = Flaky(99, ValueError("not Redis"))
    with pytest.raises(ValueError):
        await retrying(settings, other)
    assert other.calls == 1

    # Lock acquisition is tried once, and the request gets 503.
    client, _ = make_client([AIMessage(content="never")])
    fake = LockOnlyRedis()
    guard = RedisThreadGuard(fake, settings)  # type: ignore[arg-type]
    app.dependency_overrides[get_guard] = lambda: guard

    response = await client.post(RUN, json={"message": "hi"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dependency_unavailable"
    assert fake.held.acquires == 1


@pytest.mark.redis
@pytest.mark.spec("SPEC-0014", "B-0001", "AT-0004")
async def test_shared_bounded_pool(tool_context: ToolContext, redis_url: str) -> None:
    settings = redis_settings(tool_context, redis_url, redis_max_connections=3)
    services = Services()
    try:
        graph = await services.graph(settings)
        client = services.redis(settings)
        pool = client.connection_pool
        saver: Any = graph.checkpointer
    finally:
        await services.aclose()

    assert saver._redis is client
    assert isinstance(pool, BlockingConnectionPool)
    assert pool.max_connections == 3
