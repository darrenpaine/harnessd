"""Redis checkpointing, checkpoint TTL and the per-thread run lock (SPEC-0004),
over one bounded, health-checked Redis client with retries for idempotent
operations (SPEC-0014)."""

import asyncio
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

import redis.exceptions
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.redis.ashallow import AsyncShallowRedisSaver
from langgraph.checkpoint.redis.base import CHECKPOINT_PREFIX, CHECKPOINT_WRITE_PREFIX
from langgraph.checkpoint.redis.util import to_storage_safe_id, to_storage_safe_str
from redis.asyncio import BlockingConnectionPool, Redis
from redis.backoff import ExponentialWithJitterBackoff
from redis.commands.search.query import Query
from redis.exceptions import LockError
from redis.maint_notifications import MaintNotificationsConfig

from settings import Settings

LOCK_MARGIN_SECONDS = 30

# Redis is unreachable or too slow; the API answers 503 (SPEC-0002 B-0005).
REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)
# The name our connections carry in CLIENT LIST.
CLIENT_NAME = "harnessd"
# Retry delays grow from BACKOFF_BASE, with jitter, up to BACKOFF_CAP seconds.
BACKOFF_BASE = 0.1
BACKOFF_CAP = 2.0


def is_redis_error(exc: BaseException) -> bool:
    """A connection or timeout error, or one wrapped by the checkpointer's
    search layer."""
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, REDIS_ERRORS):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def build_redis(settings: Settings) -> Redis:
    """The process's one Redis client (SPEC-0014 B-0001): connect and read
    timeouts, a health check before reusing a connection idle longer than
    `REDIS_HEALTH_CHECK_SECONDS`, TCP keepalive, and a pool capped at
    `REDIS_MAX_CONNECTIONS` that waits for a free connection. It retries
    nothing itself: only idempotent operations are retried, by `retrying`."""
    pool = BlockingConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        timeout=settings.redis_socket_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        socket_keepalive=True,
        health_check_interval=settings.redis_health_check_seconds,
        client_name=CLIENT_NAME,
        # When enabled, even as "auto" without RESP3, the pool skips its check
        # that a connection it hands out is still open, so one the server
        # closed between requests would fail the next command.
        maint_notifications_config=MaintNotificationsConfig(enabled=False),
    )
    return Redis.from_pool(pool)


async def retrying[T](settings: Settings, operation: Callable[[], Awaitable[T]]) -> T:
    """Run an idempotent Redis operation, retrying it up to `REDIS_RETRIES`
    times with exponential backoff and jitter on connection and timeout errors
    (SPEC-0014 B-0002). The last error is raised, for the API's 503."""
    backoff = ExponentialWithJitterBackoff(cap=BACKOFF_CAP, base=BACKOFF_BASE)
    failures = 0
    while True:
        try:
            return await operation()
        except Exception as exc:
            if not is_redis_error(exc) or failures >= settings.redis_retries:
                raise
            failures += 1
            await asyncio.sleep(backoff.compute(failures))


class ThreadBusyError(Exception):
    """Another run holds the lock for this thread (SPEC-0004 B-0005)."""


class ThreadGuard(Protocol):
    """One run per thread at a time, and the thread's expiry clock."""

    def hold(self, thread_id: str) -> AbstractAsyncContextManager[None]:
        """Hold the thread for one run, or raise ThreadBusyError."""
        ...

    async def touch(self, thread_id: str) -> None:
        """Reset the thread's expiry after a run."""
        ...

    async def expires_in(self, thread_id: str) -> int | None:
        """Seconds until the thread expires, or None if it does not."""
        ...


def ttl_config(settings: Settings) -> dict[str, Any]:
    """Checkpointer TTL settings; the library counts in minutes."""
    return {"default_ttl": settings.checkpoint_ttl_hours * 60}


def ttl_seconds(settings: Settings) -> int:
    return int(settings.checkpoint_ttl_hours * 3600)


class RetryingSaver(AsyncShallowRedisSaver):
    """The library's shallow checkpointer, with checkpoint reads retried
    (SPEC-0014 B-0002). Writes are not retried."""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        super().__init__(redis_client=redis, ttl=ttl_config(settings))
        self.settings = settings

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        read = super().aget_tuple
        return await retrying(self.settings, lambda: read(config))


@asynccontextmanager
async def open_checkpointer(
    settings: Settings, redis: Redis | None = None
) -> AsyncGenerator[AsyncShallowRedisSaver]:
    """Checkpointer keeping only each thread's latest state (SPEC-0004 B-0001).
    It loads only the pending writes of the latest checkpoint (B-0006), which
    langgraph-checkpoint-redis does from 0.6.0.

    It uses `redis`, the client the thread lock shares (SPEC-0014 B-0001), and
    leaves it open; without one it builds its own and closes it on exit."""
    own = redis is None
    client = build_redis(settings) if redis is None else redis
    try:
        saver = RetryingSaver(client, settings)
        await retrying(settings, saver.asetup)
        yield saver
    finally:
        if own:
            await client.aclose()


# The checkpointer's search indexes, one per kind of key it writes.
INDEXES = (CHECKPOINT_PREFIX, CHECKPOINT_WRITE_PREFIX)
# As many keys as the library itself looks up per thread in adelete_thread.
MAX_KEYS = 10000


def _tag(value: str) -> str:
    """`value` escaped for a search TAG query."""
    return re.sub(r"(\W)", r"\\\1", value)


async def checkpoint_keys(redis: Redis, thread_id: str) -> list[str]:
    """Every checkpointer key of a thread, found through the checkpointer's own
    search indexes, as its `adelete_thread` finds them, never by scanning the
    keyspace (SPEC-0014 B-0004). The indexes hold the checkpoint and its writes;
    the per-namespace set of write keys is not indexed, so it is named here."""
    query = Query(f"@thread_id:{{{_tag(thread_id)}}}")
    query = query.return_fields("checkpoint_ns").paging(0, MAX_KEYS)
    keys: list[str] = []
    namespaces: set[str] = set()
    for index in INDEXES:
        result = await redis.ft(index).search(query)
        for doc in result.docs:
            keys.append(doc.id)
            namespaces.add(doc.checkpoint_ns)
    safe_id = to_storage_safe_id(thread_id)
    keys += [
        f"write_keys_zset:{safe_id}:{to_storage_safe_str(ns)}:shallow"
        for ns in sorted(namespaces)
    ]
    return keys


async def refresh_thread_ttl(redis: Redis, thread_id: str, seconds: int) -> int:
    """Reset the TTL on every checkpoint key of a thread (SPEC-0004 B-0003).

    The checkpointer sets a TTL only on keys it writes in a run, so keys from
    earlier runs would expire first. Returns the number of keys updated.
    """
    keys = await checkpoint_keys(redis, thread_id)
    async with redis.pipeline(transaction=False) as pipe:
        for key in keys:
            pipe.expire(key, seconds)
        return sum(int(done) for done in await pipe.execute())


async def thread_expiry(redis: Redis, thread_id: str) -> int | None:
    """Seconds until the thread's first key expires; None if no key expires."""
    keys = await checkpoint_keys(redis, thread_id)
    async with redis.pipeline(transaction=False) as pipe:
        for key in keys:
            pipe.ttl(key)
        ttls = await pipe.execute()
    expiring = [ttl for ttl in ttls if ttl >= 0]
    return min(expiring) if expiring else None


def lock_key(thread_id: str) -> str:
    return f"harnessd:lock:thread:{thread_id}"


@asynccontextmanager
async def thread_lock(
    redis: Redis, thread_id: str, settings: Settings
) -> AsyncGenerator[None]:
    """Hold the thread's run lock, or raise ThreadBusyError (SPEC-0004 B-0005).

    The lock expires on its own after the run timeout plus a margin, so a
    crashed run cannot block the thread forever.
    """
    lock = redis.lock(
        lock_key(thread_id),
        timeout=settings.run_timeout_seconds + LOCK_MARGIN_SECONDS,
        blocking=False,
    )
    if not await lock.acquire():
        raise ThreadBusyError(thread_id)
    try:
        yield
    finally:
        try:
            await retrying(settings, lock.release)
        except LockError:
            pass  # already expired; nothing to release


class RedisThreadGuard:
    """Thread lock and TTL refresh in Redis, shared by all workers."""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings

    def hold(self, thread_id: str) -> AbstractAsyncContextManager[None]:
        return thread_lock(self.redis, thread_id, self.settings)

    async def touch(self, thread_id: str) -> None:
        seconds = ttl_seconds(self.settings)
        await retrying(
            self.settings, lambda: refresh_thread_ttl(self.redis, thread_id, seconds)
        )

    async def expires_in(self, thread_id: str) -> int | None:
        return await retrying(
            self.settings, lambda: thread_expiry(self.redis, thread_id)
        )
