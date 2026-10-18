"""Redis checkpointing, checkpoint TTL and the per-thread run lock (SPEC-0004)."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

import redis.exceptions
from langgraph.checkpoint.redis.ashallow import AsyncShallowRedisSaver
from redis.asyncio import Redis
from redis.exceptions import LockError

from settings import Settings

LOCK_MARGIN_SECONDS = 30

# Redis is unreachable or too slow; the API answers 503 (SPEC-0002 B-0005).
REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


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


@asynccontextmanager
async def open_checkpointer(
    settings: Settings,
) -> AsyncGenerator[AsyncShallowRedisSaver]:
    """Checkpointer keeping only each thread's latest state (SPEC-0004 B-0001)."""
    async with AsyncShallowRedisSaver.from_conn_string(
        settings.redis_url, ttl=ttl_config(settings)
    ) as saver:
        await saver.asetup()
        yield saver


async def checkpoint_keys(redis: Redis, thread_id: str) -> AsyncIterator[bytes]:
    """Every checkpointer key of a thread; the thread's lock key is left out.

    Thread ids are UUIDs, so matching on the id cannot find another thread's keys.
    """
    lock = lock_key(thread_id).encode()
    async for key in redis.scan_iter(match=f"*{thread_id}*", count=1000):
        if key != lock:
            yield key


async def refresh_thread_ttl(redis: Redis, thread_id: str, seconds: int) -> int:
    """Reset the TTL on every checkpoint key of a thread (SPEC-0004 B-0003).

    The checkpointer sets a TTL only on keys it writes in a run, so keys from
    earlier runs would expire first. Returns the number of keys updated.
    """
    count = 0
    async for key in checkpoint_keys(redis, thread_id):
        count += int(await redis.expire(key, seconds))
    return count


async def thread_expiry(redis: Redis, thread_id: str) -> int | None:
    """Seconds until the thread's first key expires; None if no key expires."""
    ttls = [await redis.ttl(key) async for key in checkpoint_keys(redis, thread_id)]
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
            await lock.release()
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
        await refresh_thread_ttl(self.redis, thread_id, ttl_seconds(self.settings))

    async def expires_in(self, thread_id: str) -> int | None:
        return await thread_expiry(self.redis, thread_id)
