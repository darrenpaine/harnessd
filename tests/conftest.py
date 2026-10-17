"""Shared fixtures (SPEC-0005)."""

import logging
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage

import redis_support
from agent_support import Agent, Capture, MakeClient
from api.deps import get_graph, get_guard, get_model
from app import app
from logs import ROOT, configure_logging
from settings import LogLevel, Settings, get_settings
from tools.base import ToolContext

SECRET_ENV_VARS = ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "TAVILY_API_KEY")


@pytest.fixture(autouse=True)
def _clear_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may depend on a real API key (SPEC-0005 B-0001)."""
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def redis_url() -> str:
    """URL of a usable Redis; skips locally or fails in CI if there is none."""
    url = redis_support.redis_url()
    problem = redis_support.redis_problem(url)
    if problem is not None:
        redis_support.unavailable(problem, redis_support.running_in_ci())
    return url


async def public_resolver(host: str) -> list[str]:
    """Resolve every host to one public address, without real DNS."""
    return ["93.184.215.14"]


@pytest.fixture
def tool_context(tmp_path: Path) -> ToolContext:
    """Tool context with a temp notes file, no network and no API keys."""
    settings = Settings(_env_file=None, notes_path=tmp_path / "notes" / "notes.txt")  # type: ignore[call-arg]
    return ToolContext(settings=settings, resolve=public_resolver)


@pytest.fixture
def capture(caplog: pytest.LogCaptureFixture) -> Iterator[Capture]:
    """Configure logging at a level and route `harnessd` logs to caplog."""
    root = logging.getLogger(ROOT)
    saved = (root.level, root.propagate, list(root.handlers))
    http = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}

    def configure(level: LogLevel) -> None:
        configure_logging(Settings(_env_file=None, log_level=level))  # type: ignore[call-arg]
        if caplog.handler not in root.handlers:
            root.addHandler(caplog.handler)
        caplog.handler.setLevel(logging.DEBUG)

    yield configure
    root.setLevel(saved[0])
    root.propagate = saved[1]
    root.handlers[:] = saved[2]
    for name, level in http.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture
async def make_client(tool_context: ToolContext) -> AsyncGenerator[MakeClient]:
    """Build a client whose app runs a scripted agent on an in-memory graph."""
    clients: list[httpx.AsyncClient] = []

    def make(
        replies: list[AIMessage], **agent_options: Any
    ) -> tuple[httpx.AsyncClient, Agent]:
        agent = Agent(tool_context, replies, **agent_options)
        app.dependency_overrides[get_settings] = lambda: agent.ctx.settings
        app.dependency_overrides[get_graph] = lambda: agent.graph
        app.dependency_overrides[get_guard] = lambda: agent.guard
        app.dependency_overrides[get_model] = lambda: agent.model
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        clients.append(client)
        return client, agent

    yield make
    for client in clients:
        await client.aclose()
    app.dependency_overrides.clear()
