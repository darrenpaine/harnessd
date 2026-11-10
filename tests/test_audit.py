"""The audit events (SPEC-0008 B-0007, SPEC-0012 B-0012, B-0013): run, tool,
confirmation, guard and budget events on `harnessd.audit`, written at info
whatever LOG_LEVEL is, and holding no content."""

import dataclasses
import logging
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import UsageMetadata
from pydantic import SecretStr

from agent.spend import DailySpend
from agent_support import (
    PAGE_TEXT,
    Agent,
    Capture,
    MakeClient,
    fetch,
    note,
    tool_turn,
)
from logs import AUDIT
from settings import Settings
from tools.base import ToolContext

RUN = "/v1/agent/run"
SECRET_WORDS = "buy oat milk"  # the note text, which no event may hold
ZERO_WIDTH = "​‍"

Event = tuple[str, dict[str, Any]]


def audited(caplog: pytest.LogCaptureFixture) -> list[Event]:
    """The audit events logged, as (name, fields)."""
    return [
        (r.event, r.fields)  # type: ignore[attr-defined]
        for r in caplog.records
        if r.name == AUDIT
    ]


def used(reply: AIMessage, input_tokens: int, output_tokens: int) -> AIMessage:
    usage = UsageMetadata(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )
    return reply.model_copy(update={"usage_metadata": usage})


def priced(ctx: ToolContext, **values: Any) -> ToolContext:
    """Gemini Flash-Lite ($0.30 / $2.50 per million tokens)."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="google",
        llm_model="gemini-3.5-flash-lite",
        google_api_key=SecretStr("test-key"),
        notes_path=ctx.settings.notes_path,
        **values,
    )
    return dataclasses.replace(ctx, settings=settings)


@pytest.mark.spec("SPEC-0012", "B-0012", "B-0013", "AT-0010")
@pytest.mark.spec("SPEC-0008", "B-0007", "AT-0007")
async def test_audit_events(
    make_client: MakeClient,
    tool_context: ToolContext,
    capture: Capture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    capture("warning")
    replies = [tool_turn(note(SECRET_WORDS)), AIMessage(content="noted")]
    client, _ = make_client(replies)

    paused = await client.post(
        RUN, json={"message": f"note {SECRET_WORDS}"}, headers={"X-Request-ID": "r-1"}
    )
    thread = paused.json()["thread_id"]
    done = await client.post(
        RUN,
        json={"thread_id": thread, "confirm": True},
        headers={"X-Request-ID": "r-2"},
    )

    assert done.json()["status"] == "ok"
    events = audited(caplog)
    assert [name for name, _ in events] == [
        "run.start",
        "confirm.wait",
        "run.end",
        "run.start",
        "confirm.decision",
        "tool.call",
        "run.end",
    ]
    assert [fields["request"] for _, fields in events] == ["r-1"] * 3 + ["r-2"] * 4
    assert all(fields["thread"] == thread for _, fields in events)
    assert events[1][1]["tool"] == "add_note"
    assert events[2][1]["status"] == "awaiting_confirmation"
    assert events[4][1]["approved"] is True
    assert events[5][1]["result"] == "ok"
    # Only audit events are written at warning, and none holds content.
    assert all(r.name == AUDIT for r in caplog.records)
    assert not any(SECRET_WORDS in r.getMessage() for r in caplog.records)

    # A run over its token budget: budget.stop, then run.end with its usage.
    caplog.clear()
    ctx = priced(tool_context, max_tokens_per_run=100)
    script = [
        used(tool_turn(fetch("a")), 40, 20),
        used(tool_turn(fetch("b")), 50, 10),  # takes the run to 120
    ]
    response = await Agent(ctx, script).run(message="read a and b")

    assert response.error is not None and response.error.code == "budget_exceeded"
    stop, end = [fields for name, fields in audited(caplog) if name != "tool.call"][1:]
    assert stop == stop | {"scope": "run", "used": 120, "limit": 100}
    # 90 input x $0.30 + 30 output x $2.50 per million: 27 + 75 micro-dollars.
    assert end == end | {
        "status": "error",
        "tokens": 120,
        "estimated": False,
        "cost_usd": 0.000102,
        "error": "budget_exceeded",
    }


@pytest.mark.spec("SPEC-0012", "B-0008", "B-0013")
async def test_run_end_usage(
    tool_context: ToolContext, capture: Capture, caplog: pytest.LogCaptureFixture
) -> None:
    """Estimated tokens are marked; an unpriced model has no cost_usd."""
    capture("warning")
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="anthropic",
        llm_model="claude-unpriced",
        anthropic_api_key=SecretStr("test-key"),
        notes_path=tool_context.settings.notes_path,
    )
    ctx = dataclasses.replace(tool_context, settings=settings)

    await Agent(ctx, [AIMessage(content="hello there")]).run(message="hi")

    [end] = [fields for name, fields in audited(caplog) if name == "run.end"]
    assert end["estimated"] is True and end["tokens"] > 0
    assert "cost_usd" not in end


@pytest.mark.spec("SPEC-0012", "B-0013", "B-0015")
async def test_stripped_logged(
    tool_context: ToolContext, capture: Capture, caplog: pytest.LogCaptureFixture
) -> None:
    capture("warning")
    page = f"page{ZERO_WIDTH}text"
    agent = Agent(
        tool_context,
        [tool_turn(fetch("a")), AIMessage(content="done")],
        serve=lambda _: _text(page),
    )

    await agent.run(message=f"read{ZERO_WIDTH} a")

    guards = [fields for name, fields in audited(caplog) if name == "guard"]
    assert [(g["check"], g["action"], g["count"]) for g in guards] == [
        ("invisible_chars", "stripped", 2),  # the message
        ("invisible_chars", "stripped", 2),  # the page
    ]
    assert guards[1]["tool"] == "fetch_site"
    assert guards[1]["call_id"] == "call-fetch_site-0"

    caplog.clear()
    await Agent(tool_context, [AIMessage(content="hi")]).run(message="plain")
    assert "guard" not in [name for name, _ in audited(caplog)]


class FixedScreen:
    def __init__(self, score: float | None) -> None:
        self.score = score

    async def probability(self, text: str) -> float:
        if self.score is None:
            raise RuntimeError("screen down")
        return self.score


@pytest.mark.spec("SPEC-0012", "B-0005", "B-0006", "B-0013")
@pytest.mark.parametrize(
    ("score", "on_error", "expected"),
    [
        (0.9, "withhold", {"probability": 0.9}),
        (0.5, "withhold", None),
        (None, "withhold", {"error": True}),
        (None, "allow", None),
    ],
)
async def test_withheld_logged(
    tool_context: ToolContext,
    capture: Capture,
    caplog: pytest.LogCaptureFixture,
    score: float | None,
    on_error: str,
    expected: dict[str, Any] | None,
) -> None:
    capture("warning")
    settings = tool_context.settings.model_copy(
        update={"injection_screen_on_error": on_error}
    )
    ctx = dataclasses.replace(tool_context, settings=settings)
    replies = [tool_turn(fetch("a")), AIMessage(content="done")]

    await Agent(ctx, replies, screen=FixedScreen(score)).run(message="read a")

    guards = [fields for name, fields in audited(caplog) if name == "guard"]
    if expected is None:
        assert guards == []
        return
    [guard] = guards
    assert guard == guard | {
        "tool": "fetch_site",
        "check": "injection_screen",
        "action": "withheld",
        **expected,
    }
    assert PAGE_TEXT not in str(guard)


@pytest.mark.spec("SPEC-0012", "B-0010", "B-0013")
async def test_day_stop_logged(
    make_client: MakeClient,
    tool_context: ToolContext,
    capture: Capture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A request refused with 429 logs budget.stop with the day's spend in USD."""
    capture("warning")
    ctx = priced(tool_context, daily_cost_limit_usd=0.01)
    spend = DailySpend(MemoryStore(12_500), ctx.settings)
    client, _ = make_client([], ctx=ctx, spend=spend)

    response = await client.post(RUN, json={"message": "hi"})

    assert response.status_code == 429
    [(name, fields)] = audited(caplog)
    assert name == "budget.stop"
    assert fields == fields | {"scope": "day", "used": 0.0125, "limit": 0.01}


class MemoryStore:
    """Every day's spend is `total` micro-dollars."""

    def __init__(self, total: int) -> None:
        self._total = total

    async def total(self, key: str) -> int:
        return self._total

    async def add(self, key: str, micro_usd: int) -> int:
        self._total += micro_usd
        return self._total


def _text(body: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/plain"}, text=body)


def test_audit_logger_ignores_level(capture: Capture) -> None:
    """LOG_LEVEL=error leaves harnessd.audit at info (SPEC-0008 B-0007)."""
    capture("error")
    assert logging.getLogger(AUDIT).getEffectiveLevel() == logging.INFO
    assert logging.getLogger("harnessd.graph").getEffectiveLevel() == logging.ERROR
