"""The daily cost limit (SPEC-0012 B-0010).

`DailySpend` prices each model call and adds it to a counter for the UTC day.
The counter lives in a `SpendStore`: Redis in the app, so every instance shares
it. The limit is checked before a call and spend added after it, so runs at the
same time can each go over by up to one call.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from redis.asyncio import Redis

from agent.budgets import Tokens
from agent.prices import MICRO_USD, cost_micro_usd, price_of
from settings import Settings

KEY_PREFIX = "harnessd:spend:"
KEY_TTL_SECONDS = 48 * 3600


class DailyLimitError(Exception):
    """The day's spend has reached `DAILY_COST_LIMIT_USD`: a 429 before the lock."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SpendStore(Protocol):
    async def total(self, key: str) -> int:
        """The key's micro-dollars; 0 when it has none."""
        ...

    async def add(self, key: str, micro_usd: int) -> int:
        """Add to the key and return its new total."""
        ...


class RedisSpendStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def total(self, key: str) -> int:
        value = await self._redis.get(key)
        return int(value) if value is not None else 0

    async def add(self, key: str, micro_usd: int) -> int:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incrby(key, micro_usd)
            pipe.expire(key, KEY_TTL_SECONDS)
            total, _ = await pipe.execute()
        return int(total)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DailySpend:
    def __init__(
        self,
        store: SpendStore,
        settings: Settings,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if settings.daily_cost_limit_usd is None:
            raise ValueError("DAILY_COST_LIMIT_USD is not set")
        self._store = store
        self._provider = settings.llm_provider
        self._limit_usd = settings.daily_cost_limit_usd
        self._limit = round(settings.daily_cost_limit_usd * MICRO_USD)
        self._now = now

    async def stop(self) -> str | None:
        """The message to end on when the day's spend has reached the limit."""
        return self._message(await self._store.total(self._key()))

    async def refuse_if_reached(self) -> None:
        """Raise DailyLimitError when the limit is reached (before the lock)."""
        if message := await self.stop():
            raise DailyLimitError(message, self.retry_after())

    async def add(self, model: str, tokens: Tokens) -> str | None:
        """Add one call's cost; the stop message if the day is now at the limit.
        Settings ensure every model in use has a price (B-0011)."""
        price = price_of(self._provider, model)
        if price is None:
            raise ValueError(f"no price for {self._provider} {model}")
        cost = cost_micro_usd(price, tokens.input, tokens.output)
        key = self._key()
        total = (
            await self._store.add(key, cost) if cost else await self._store.total(key)
        )
        return self._message(total)

    def retry_after(self) -> int:
        """Whole seconds until 00:00 UTC, at least 1."""
        now = self._now()
        midnight = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(
            days=1
        )
        return max(1, int((midnight - now).total_seconds() + 0.999))

    def _key(self) -> str:
        return f"{KEY_PREFIX}{self._now():%Y-%m-%d}"

    def _message(self, total: int) -> str | None:
        if total < self._limit:
            return None
        return (
            f"the daily cost limit of ${self._limit_usd:.2f} was reached; "
            "it resets at 00:00 UTC"
        )


def build_spend(redis: Redis, settings: Settings) -> DailySpend | None:
    """The day's spend counter, or None when there is no daily limit."""
    if settings.daily_cost_limit_usd is None:
        return None
    return DailySpend(RedisSpendStore(redis), settings)
