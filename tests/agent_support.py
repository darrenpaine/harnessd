"""Build an agent runner on a scripted model and an in-memory checkpointer."""

import asyncio
import dataclasses
import time
from collections import defaultdict
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.graph import build_graph
from agent.metered import MeteredModel
from agent.persistence import ThreadBusyError, ThreadGuard
from agent.runner import AgentRunner
from agent.screen import Screen
from agent.spend import DailySpend
from fakes import ScriptedChatModel
from schemas import AgentRequest, AgentResponse
from settings import LogLevel, Settings
from tools import TOOLS, add_note
from tools.base import ToolContext
from tracing import Tracing

PAGE_TEXT = "page text"

# The `capture` fixture: configure logging at a level and route it to caplog.
Capture = Callable[[LogLevel], None]

# The `make_client` fixture: (scripted replies, Agent options) -> (client, agent).
MakeClient = Callable[..., tuple[httpx.AsyncClient, "Agent"]]

# The `start_tracing` fixture: (exporter, enabled=, content=) -> Tracing.
StartTracing = Callable[..., Tracing]


def finished_spans(
    tracing: Tracing, exporter: InMemorySpanExporter
) -> list[ReadableSpan]:
    """Every span ended so far, after flushing the batch processor."""
    tracing.force_flush()
    return list(exporter.get_finished_spans())


class LocalThreadGuard:
    """In-process guard for graphs with an in-memory checkpointer."""

    def __init__(self) -> None:
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @asynccontextmanager
    async def hold(self, thread_id: str) -> AsyncGenerator[None]:
        lock = self._locks[thread_id]
        if lock.locked():
            raise ThreadBusyError(thread_id)
        async with lock:
            yield

    async def touch(self, thread_id: str) -> None:
        pass  # in-memory state does not expire

    async def expires_in(self, thread_id: str) -> int | None:
        return None


class MemorySpendStore:
    """A `SpendStore` in memory: micro-dollars per day key."""

    def __init__(self, totals: dict[str, int] | None = None) -> None:
        self.totals = totals or {}

    async def total(self, key: str) -> int:
        return self.totals.get(key, 0)

    async def add(self, key: str, micro_usd: int) -> int:
        self.totals[key] = self.totals.get(key, 0) + micro_usd
        return self.totals[key]


def metered(model: BaseChatModel) -> MeteredModel:
    """`model` as a metered model on default settings: free, with no daily limit."""
    return MeteredModel(model, Settings(_env_file=None))  # type: ignore[call-arg]


def tool_turn(*calls: tuple[str, dict[str, Any]], text: str = "") -> AIMessage:
    """A model reply requesting `calls`, given as (tool name, args) pairs."""
    return AIMessage(
        content=text,
        tool_calls=[
            {"name": name, "args": args, "id": f"call-{name}-{index}"}
            for index, (name, args) in enumerate(calls)
        ],
    )


def fetch(path: str = "") -> tuple[str, dict[str, Any]]:
    return ("fetch_site", {"url": f"https://example.com/{path}"})


def note(text: str) -> tuple[str, dict[str, Any]]:
    return ("add_note", {"text": text})


def slow_note_writes(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Make add_note's file write take `seconds`. It runs in a worker thread,
    which a timeout doesn't stop, so the note is still written."""
    write = add_note._append

    def slow(path: Path, line: str) -> None:
        time.sleep(seconds)
        write(path, line)

    monkeypatch.setattr(add_note, "_append", slow)


class Agent:
    """A runner over the real tools, with their HTTP served by a mock transport:
    PAGE_TEXT as text/plain, or `serve` when given."""

    def __init__(
        self,
        ctx: ToolContext,
        replies: list[AIMessage],
        *,
        delay: float = 0,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        guard: ThreadGuard | None = None,
        serve: Callable[[httpx.Request], httpx.Response] | None = None,
        screen: Screen | None = None,
        spend: DailySpend | None = None,
    ) -> None:
        self.fetched: list[str] = []
        self._custom = serve
        self.model = ScriptedChatModel(replies=replies, delay=delay)
        self.ctx = dataclasses.replace(ctx, transport=httpx.MockTransport(self._serve))
        self.metered = MeteredModel(self.model, ctx.settings, spend)
        self.graph = build_graph(
            self.metered,
            self.ctx,
            TOOLS,
            checkpointer or InMemorySaver(),
            screen=screen,
        )
        self.guard = guard or LocalThreadGuard()
        self.runner = AgentRunner(self.graph, ctx.settings, self.guard, self.metered)

    def _serve(self, request: httpx.Request) -> httpx.Response:
        self.fetched.append(str(request.url))
        if self._custom is not None:
            return self._custom(request)
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, text=PAGE_TEXT
        )

    async def run(self, **fields: Any) -> AgentResponse:
        return await self.runner.run(AgentRequest.model_validate(fields))

    def tool_results(self, call: int) -> list[ToolMessage]:
        """Tool results the model received on its `call`-th call (0-based)."""
        return [m for m in self.model.calls[call] if isinstance(m, ToolMessage)]

    def last_input(self, call: int) -> BaseMessage:
        return self.model.calls[call][-1]
