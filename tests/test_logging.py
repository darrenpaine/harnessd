"""Log level setting and run events (SPEC-0008)."""

import asyncio
import dataclasses
import json
import logging
import re
from collections.abc import AsyncGenerator, Callable
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from ollama import ResponseError as OllamaResponseError
from pydantic import SecretStr, ValidationError
from starlette.types import Message, Receive, Scope, Send

from agent.graph import build_graph
from agent_support import (
    PAGE_TEXT,
    Agent,
    Capture,
    LocalThreadGuard,
    fetch,
    note,
    tool_turn,
)
from api.deps import get_graph, get_guard
from api.errors import InternalErrorMiddleware
from app import app
from fakes import RaisingChatModel
from logs import (
    ROOT,
    JsonFormatter,
    clip,
    configure_logging,
    event,
    formatter,
    log_context,
)
from settings import LogLevel, Settings, get_settings
from tools import TOOLS
from tools.base import ToolContext

RUN = "/v1/agent/run"


def lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name.startswith(ROOT)]


def names(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Event names, with the node for graph.step and status for run.end."""
    result = []
    for line in lines(caplog):
        name, *fields = line.split(" ")
        values = dict(field.split("=", 1) for field in fields)
        if name == "graph.step":
            name += f" {values['node']}"
        if name == "run.end":
            name += f" {values['status']}"
        result.append(name)
    return result


@pytest.mark.spec("SPEC-0008", "B-0001", "AT-0001")
@pytest.mark.parametrize(
    ("value", "expected"), [("DEBUG", "debug"), ("warning", "warning"), (None, "info")]
)
def test_log_level_setting(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str
) -> None:
    if value is None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("LOG_LEVEL", value)
    assert Settings(_env_file=None).log_level == expected  # type: ignore[call-arg]


@pytest.mark.spec("SPEC-0008", "B-0001", "AT-0001")
def test_unknown_log_level_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "loud")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.spec("SPEC-0008", "B-0001")
def test_level_applied(capture: Capture) -> None:
    capture("warning")
    assert logging.getLogger(f"{ROOT}.runner").getEffectiveLevel() == logging.WARNING
    capture("debug")
    assert logging.getLogger(f"{ROOT}.runner").getEffectiveLevel() == logging.DEBUG
    harnessd = [h for h in logging.getLogger(ROOT).handlers if hasattr(h, "harnessd")]
    assert len(harnessd) == 1


@pytest.mark.spec("SPEC-0008", "B-0002", "B-0003", "AT-0002")
async def test_run_events_logged(
    capture: Capture, caplog: pytest.LogCaptureFixture, tool_context: ToolContext
) -> None:
    capture("info")
    search = ("web_search", {"query": "python"})
    agent = Agent(tool_context, [tool_turn(search), AIMessage(content="done")])

    response = await agent.run(message="search")

    assert names(caplog) == [
        "run.start",
        "graph.step model",
        "model.reply",
        "graph.step tools",
        "tool.call",
        "graph.step model",
        "model.reply",
        "run.end ok",
    ]
    for line in lines(caplog):
        assert f"thread={response.thread_id}" in line.split(" ")
    start, *_, end = lines(caplog)
    assert "kind=message" in start and "provider=ollama" in start
    assert "tool_calls=1" in end
    call = next(line for line in lines(caplog) if line.startswith("tool.call"))
    assert "tool=web_search" in call and "result=upstream_error" in call


@pytest.mark.spec("SPEC-0008", "B-0002", "AT-0003")
async def test_confirmation_events_logged(
    capture: Capture, caplog: pytest.LogCaptureFixture, tool_context: ToolContext
) -> None:
    capture("info")
    agent = Agent(tool_context, [tool_turn(note("milk")), AIMessage(content="ok")])

    paused = await agent.run(message="note milk")
    assert names(caplog)[-2:] == ["confirm.wait", "run.end awaiting_confirmation"]
    assert "tool=add_note" in lines(caplog)[-2]
    caplog.clear()

    await agent.run(thread_id=paused.thread_id, confirm=True)
    assert names(caplog)[:2] == ["run.start", "confirm.decision"]
    assert "approved=true" in lines(caplog)[1]
    assert "kind=confirm" in lines(caplog)[0]
    assert "result=ok" in next(x for x in lines(caplog) if x.startswith("tool.call"))


@pytest.mark.spec("SPEC-0008", "B-0004", "AT-0004")
@pytest.mark.parametrize("level", ["info", "debug"])
async def test_no_content_or_secrets_logged(
    capture: Capture,
    caplog: pytest.LogCaptureFixture,
    tool_context: ToolContext,
    level: LogLevel,
) -> None:
    capture(level)
    settings = tool_context.settings.model_copy(
        update={"tavily_api_key": SecretStr("KEY-MARKER")}
    )
    ctx = dataclasses.replace(tool_context, settings=settings)
    replies = [
        tool_turn(fetch("INPUT-MARKER"), text="REASONING-MARKER"),
        AIMessage(content="ANSWER-MARKER"),
    ]
    agent = Agent(ctx, replies)

    await agent.run(message="MESSAGE-MARKER")

    text = "\n".join(lines(caplog))
    for marker in ("MESSAGE-MARKER", "REASONING-MARKER", "ANSWER-MARKER"):
        assert marker not in text
    assert "KEY-MARKER" not in text and PAGE_TEXT not in text
    assert ("INPUT-MARKER" in text) == (level == "debug")


@pytest.mark.spec("SPEC-0008", "B-0004")
def test_event_format(capture: Capture, caplog: pytest.LogCaptureFixture) -> None:
    capture("debug")
    log = logging.getLogger(f"{ROOT}.test")
    with log_context(thread="t-1"):
        event(log, logging.INFO, "x.y", n=1, flag=True, text="a b", skipped=None)
    event(log, logging.INFO, "x.z", input=clip({"q": "a" * 500}))

    first, second = lines(caplog)
    assert first == 'x.y thread=t-1 n=1 flag=true text="a b"'
    assert len(second.removeprefix("x.z input=")) == 200


@pytest.mark.spec("SPEC-0008", "B-0003")
def test_event_fields_kept_on_record(
    capture: Capture, caplog: pytest.LogCaptureFixture
) -> None:
    """The name and fields stay separate on the record, for other formatters."""
    capture("info")
    log = logging.getLogger(f"{ROOT}.test")
    with log_context(request="r-1"), log_context(thread="t-1"):
        event(log, logging.INFO, "x.y", n=1, thread="t-2", skipped=None)
    event(log, logging.INFO, "x.z")

    first, second = [r for r in caplog.records if r.name.startswith(ROOT)]
    assert first.event == "x.y"  # type: ignore[attr-defined]
    assert first.fields == {"request": "r-1", "thread": "t-2", "n": 1}  # type: ignore[attr-defined]
    assert first.getMessage() == "x.y request=r-1 thread=t-2 n=1"
    assert second.fields == {}  # type: ignore[attr-defined]


@pytest.fixture
async def api_client(
    tool_context: ToolContext,
) -> AsyncGenerator[Callable[[Any], httpx.AsyncClient]]:
    """A client for an app whose graph uses the given chat model."""
    clients: list[httpx.AsyncClient] = []

    def make(model: Any) -> httpx.AsyncClient:
        graph = build_graph(model, tool_context, TOOLS, InMemorySaver())
        guard = LocalThreadGuard()
        app.dependency_overrides[get_settings] = lambda: tool_context.settings
        app.dependency_overrides[get_graph] = lambda: graph
        app.dependency_overrides[get_guard] = lambda: guard
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        client = httpx.AsyncClient(transport=transport, base_url="http://t")
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()
    app.dependency_overrides.clear()


@pytest.mark.spec("SPEC-0008", "B-0005", "AT-0005")
async def test_error_responses_logged(
    capture: Capture,
    caplog: pytest.LogCaptureFixture,
    api_client: Callable[[Any], httpx.AsyncClient],
) -> None:
    capture("info")
    failing = api_client(RaisingChatModel(error=RuntimeError("boom")))
    assert (await failing.post(RUN, json={"message": "hi"})).status_code == 500
    failed = [r for r in caplog.records if r.getMessage().startswith("request.failed")]
    assert len(failed) == 1
    assert failed[0].levelno == logging.ERROR
    assert failed[0].exc_info is not None
    caplog.clear()

    refused = api_client(RaisingChatModel(error=OllamaResponseError("no", 404)))
    assert (await refused.post(RUN, json={"message": "hi"})).status_code == 503
    [record] = [r for r in caplog.records if r.name.startswith(ROOT)][-1:]
    assert record.levelno == logging.WARNING
    name, *fields = record.getMessage().split(" ")
    assert name == "dependency.unavailable"
    assert fields[0].startswith("request=") and "cause=" in record.getMessage()


@pytest.mark.spec("SPEC-0008", "B-0005")
async def test_busy_thread_logged(
    capture: Capture,
    caplog: pytest.LogCaptureFixture,
    tool_context: ToolContext,
) -> None:
    capture("info")
    agent = Agent(tool_context, [AIMessage(content="a")], delay=0.2)
    app.dependency_overrides[get_settings] = lambda: tool_context.settings
    app.dependency_overrides[get_graph] = lambda: agent.graph
    app.dependency_overrides[get_guard] = lambda: agent.guard
    thread_id = "6f1c2a9e-4b7d-4e2a-9c1f-3d5e8a7b2c10"
    body = {"thread_id": thread_id, "message": "hi"}
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            first = asyncio.create_task(c.post(RUN, json=body))
            await asyncio.sleep(0.05)
            assert (await c.post(RUN, json=body)).status_code == 409
            await first
    finally:
        app.dependency_overrides.clear()

    busy = [r for r in caplog.records if r.getMessage().startswith("thread.busy")]
    assert len(busy) == 1
    assert busy[0].levelno == logging.WARNING
    assert f"thread={thread_id}" in busy[0].getMessage()


@pytest.mark.spec("SPEC-0008", "B-0006", "AT-0006")
@pytest.mark.parametrize(
    ("level", "expected"),
    [("info", logging.WARNING), ("warning", logging.WARNING), ("debug", logging.DEBUG)],
)
def test_http_client_logs_quiet(
    capture: Capture, level: LogLevel, expected: int
) -> None:
    capture(level)
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() == expected


async def _receive() -> Message:
    return {"type": "http.request", "body": b""}


@pytest.mark.spec("SPEC-0002", "B-0008")
async def test_middleware_passes_other_scopes_and_late_errors(
    capture: Capture, caplog: pytest.LogCaptureFixture
) -> None:
    capture("info")
    seen: list[str] = []
    sent: list[Message] = []

    async def lifespan_app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def started_then_failed(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("late")

    async def collect(message: Message) -> None:
        sent.append(message)

    await InternalErrorMiddleware(lifespan_app)({"type": "lifespan"}, _receive, collect)
    assert seen == ["lifespan"]

    middleware = InternalErrorMiddleware(started_then_failed)
    with pytest.raises(RuntimeError):
        await middleware({"type": "http", "path": "/x"}, _receive, collect)
    assert [m["type"] for m in sent] == ["http.response.start"]
    assert names(caplog) == ["request.failed"]


@pytest.mark.spec("SPEC-0010", "B-0008", "AT-0008")
def test_log_format(capture: Capture, caplog: pytest.LogCaptureFixture) -> None:
    capture("info")
    log = logging.getLogger(f"{ROOT}.test")
    with log_context(request="r-1"), log_context(thread="t-1"):
        event(log, logging.INFO, "run.start", kind="message", max_tool_calls=5)
    log.warning("plain %s", "message")
    record, plain = [r for r in caplog.records if r.name.startswith(ROOT)]

    json_settings = Settings(_env_file=None, log_format="json")  # type: ignore[call-arg]
    as_json = json.loads(formatter(json_settings).format(record))
    assert as_json == {
        "time": as_json["time"],
        "level": "info",
        "logger": "harnessd.test",
        "event": "run.start",
        "request_id": "r-1",
        "thread": "t-1",
        "kind": "message",
        "max_tool_calls": 5,
    }
    assert list(as_json)[:5] == ["time", "level", "logger", "event", "request_id"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", as_json["time"])
    plain_json = json.loads(JsonFormatter().format(plain))
    assert plain_json["message"] == "plain message" and "event" not in plain_json

    text = formatter(Settings(_env_file=None)).format(record)  # type: ignore[call-arg]
    assert text.endswith(
        "INFO harnessd.test run.start request=r-1 thread=t-1 kind=message "
        "max_tool_calls=5"
    )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_format="xml")  # type: ignore[call-arg, arg-type]


@pytest.mark.spec("SPEC-0010", "B-0008")
def test_log_format_applied(capture: Capture) -> None:
    upper = Settings(_env_file=None, log_format="JSON")  # type: ignore[call-arg, arg-type]
    configure_logging(upper)
    [handler] = [h for h in logging.getLogger(ROOT).handlers if hasattr(h, "harnessd")]
    assert isinstance(handler.formatter, JsonFormatter)
    configure_logging(Settings(_env_file=None))  # type: ignore[call-arg]
    assert not isinstance(handler.formatter, JsonFormatter)
