"""Shared services and the FastAPI dependencies routes use (SPEC-0002).

Tests replace `get_graph`, `get_guard` and `get_metered` through
`app.dependency_overrides`.
"""

import asyncio
import time
from contextlib import AsyncExitStack
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from langchain_core.language_models import BaseChatModel
from redis.asyncio import Redis

from agent.graph import AgentGraph, build_graph
from agent.llm import build_chat_model
from agent.metered import MeteredModel
from agent.persistence import (
    REDIS_ERRORS,
    RedisThreadGuard,
    ThreadGuard,
    build_redis,
    open_checkpointer,
)
from agent.runner import AgentRunner
from agent.screen import Screen, build_screen
from agent.spend import DailySpend, build_spend
from agent.threads import ThreadService
from logs import get_logger
from settings import Settings, get_settings
from tools import TOOLS
from tools.base import ToolContext

log = get_logger("app")

SettingsDep = Annotated[Settings, Depends(get_settings)]


class Services:
    """Redis client, chat model and agent graph, created on first use.

    Created lazily so the app starts, and /health answers, while Redis is down;
    requests then get 503 until Redis is back (SPEC-0002 B-0005).

    One Redis client serves the checkpointer, the thread lock and the day's
    spend (SPEC-0014 B-0001). After a Redis failure the next request rebuilds
    the checkpointer and graph, at most once per `REDIS_HEALTH_CHECK_SECONDS`
    (SPEC-0014 B-0003).
    """

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._lock = asyncio.Lock()
        self._redis: Redis | None = None
        self._model: BaseChatModel | None = None
        self._metered: MeteredModel | None = None
        self._graph: AgentGraph | None = None
        self._screen: Screen | None = None
        self._screen_built = False
        self._failed = False
        self._built_at = 0.0

    def redis(self, settings: Settings) -> Redis:
        if self._redis is None:
            self._redis = build_redis(settings)
        return self._redis

    def model(self, settings: Settings) -> BaseChatModel:
        if self._model is None:
            self._model = build_chat_model(settings)
        return self._model

    def spend(self, settings: Settings) -> DailySpend | None:
        """The day's spend, or None with no daily limit (SPEC-0012 B-0010)."""
        return build_spend(self.redis(settings), settings)

    def metered(self, settings: Settings) -> MeteredModel:
        """The agent's metered model: every call through it is counted, priced
        and added to the day's spend (SPEC-0012 B-0010)."""
        if self._metered is None:
            self._metered = MeteredModel(
                self.model(settings), settings, self.spend(settings)
            )
        return self._metered

    def screen(self, settings: Settings) -> Screen | None:
        """The injection screen, or None when it is off (SPEC-0012 B-0005)."""
        if not self._screen_built:
            self._screen = build_screen(settings, self.spend(settings))
            self._screen_built = True
        return self._screen

    def redis_failed(self) -> None:
        """A request failed on Redis: rebuild the checkpointer next time."""
        self._failed = True

    async def graph(self, settings: Settings) -> AgentGraph:
        async with self._lock:
            if self._graph is None or self._rebuild_due(settings):
                self._graph = await self._build_graph(settings)
            return self._graph

    def _rebuild_due(self, settings: Settings) -> bool:
        waited = time.monotonic() - self._built_at
        return self._failed and waited >= settings.redis_health_check_seconds

    async def _build_graph(self, settings: Settings) -> AgentGraph:
        """A new checkpointer and graph. A failed build keeps the old graph,
        and the next try waits a full interval."""
        self._built_at = time.monotonic()
        stack = AsyncExitStack()
        saver = await stack.enter_async_context(
            open_checkpointer(settings, self.redis(settings))
        )
        old, self._stack, self._failed = self._stack, stack, False
        await old.aclose()
        return build_graph(
            self.metered(settings),
            ToolContext(settings=settings),
            TOOLS,
            saver,
            screen=self.screen(settings),
        )

    async def aclose(self) -> None:
        """Close the checkpointer and the Redis client.

        Redis may already be gone at shutdown. That is logged, not raised, so
        shutdown finishes and the client is closed even if the checkpointer fails.
        """
        try:
            await self._stack.aclose()
        except REDIS_ERRORS as exc:
            log.warning("closing the checkpointer failed: %r", exc)
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except REDIS_ERRORS as exc:
                log.warning("closing the Redis client failed: %r", exc)


def app_settings(app: FastAPI) -> Settings:
    """Settings for code outside a request's dependencies (lifespan, error
    handlers), honouring a test's `get_settings` override like the routes do."""
    settings: Settings = app.dependency_overrides.get(get_settings, get_settings)()
    return settings


async def get_graph(request: Request, settings: SettingsDep) -> AgentGraph:
    """The compiled agent graph; tests override this (SPEC-0002)."""
    services: Services = request.app.state.services
    return await services.graph(settings)


def get_metered(request: Request, settings: SettingsDep) -> MeteredModel:
    """The agent's metered model, also used for calls outside the graph such as
    compaction; tests override this (SPEC-0012 B-0010)."""
    services: Services = request.app.state.services
    return services.metered(settings)


def get_guard(request: Request, settings: SettingsDep) -> ThreadGuard:
    services: Services = request.app.state.services
    return RedisThreadGuard(services.redis(settings), settings)


MeteredDep = Annotated[MeteredModel, Depends(get_metered)]


def get_runner(
    graph: Annotated[AgentGraph, Depends(get_graph)],
    guard: Annotated[ThreadGuard, Depends(get_guard)],
    settings: SettingsDep,
    model: MeteredDep,
) -> AgentRunner:
    return AgentRunner(graph, settings, guard, model)


def get_threads(
    graph: Annotated[AgentGraph, Depends(get_graph)],
    model: MeteredDep,
    guard: Annotated[ThreadGuard, Depends(get_guard)],
    settings: SettingsDep,
) -> ThreadService:
    return ThreadService(graph, model, settings, guard)
