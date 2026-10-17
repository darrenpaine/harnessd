"""Shared services and the FastAPI dependencies routes use (SPEC-0002).

Tests replace `get_graph`, `get_guard` and `get_model` through
`app.dependency_overrides`.
"""

import asyncio
from contextlib import AsyncExitStack
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from langchain_core.language_models import BaseChatModel
from redis.asyncio import Redis

from agent.graph import AgentGraph, build_graph
from agent.llm import build_chat_model
from agent.persistence import (
    REDIS_ERRORS,
    RedisThreadGuard,
    ThreadGuard,
    open_checkpointer,
)
from agent.runner import AgentRunner
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
    """

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._lock = asyncio.Lock()
        self._redis: Redis | None = None
        self._model: BaseChatModel | None = None
        self._graph: AgentGraph | None = None

    def redis(self, settings: Settings) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(settings.redis_url)
        return self._redis

    def model(self, settings: Settings) -> BaseChatModel:
        if self._model is None:
            self._model = build_chat_model(settings)
        return self._model

    async def graph(self, settings: Settings) -> AgentGraph:
        async with self._lock:
            if self._graph is None:
                saver = await self._stack.enter_async_context(
                    open_checkpointer(settings)
                )
                self._graph = build_graph(
                    self.model(settings),
                    ToolContext(settings=settings),
                    TOOLS,
                    saver,
                )
            return self._graph

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


def get_model(request: Request, settings: SettingsDep) -> BaseChatModel:
    """The chat model, for calls outside the graph such as compaction."""
    services: Services = request.app.state.services
    return services.model(settings)


def get_guard(request: Request, settings: SettingsDep) -> ThreadGuard:
    services: Services = request.app.state.services
    return RedisThreadGuard(services.redis(settings), settings)


def get_runner(
    graph: Annotated[AgentGraph, Depends(get_graph)],
    guard: Annotated[ThreadGuard, Depends(get_guard)],
    settings: SettingsDep,
) -> AgentRunner:
    return AgentRunner(graph, settings, guard)


def get_threads(
    graph: Annotated[AgentGraph, Depends(get_graph)],
    model: Annotated[BaseChatModel, Depends(get_model)],
    guard: Annotated[ThreadGuard, Depends(get_guard)],
    settings: SettingsDep,
) -> ThreadService:
    return ThreadService(graph, model, settings, guard)
