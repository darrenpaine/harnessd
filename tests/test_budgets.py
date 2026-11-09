"""Token budgets per run and per thread, and the daily cost limit (SPEC-0012
B-0008..B-0011, B-0014).

A scripted model reports the usage of each reply, so the counts are exact.
"""

import dataclasses
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.messages.ai import UsageMetadata
from pydantic import SecretStr
from redis.asyncio import Redis

from agent.budgets import Tokens, call_tokens
from agent.prices import FREE, cost_micro_usd, price_of
from agent.screen import ChatScreen
from agent.spend import DailyLimitError, DailySpend, RedisSpendStore, SpendStore
from agent_support import (
    Agent,
    Capture,
    LocalThreadGuard,
    MakeClient,
    fetch,
    note,
    tool_turn,
)
from fakes import ScriptedChatModel
from logs import ROOT
from settings import Settings
from tools.base import ToolContext

RUN = "/v1/agent/run"


@pytest.fixture
def tool_context(tool_context: ToolContext) -> ToolContext:
    """Small budgets: 100 tokens per run, 150 per thread."""
    settings = tool_context.settings.model_copy(
        update={"max_tokens_per_run": 100, "max_tokens_per_thread": 150}
    )
    return dataclasses.replace(tool_context, settings=settings)


def used(reply: AIMessage, tokens: int) -> AIMessage:
    """The reply, reporting `tokens` of usage (all of it output)."""
    usage = UsageMetadata(input_tokens=0, output_tokens=tokens, total_tokens=tokens)
    return reply.model_copy(update={"usage_metadata": usage})


def answer(text: str, tokens: int) -> AIMessage:
    return used(AIMessage(content=text), tokens)


@pytest.mark.spec("SPEC-0012", "B-0008", "B-0009", "AT-0006")
async def test_run_token_budget(tool_context: ToolContext) -> None:
    script = [
        used(tool_turn(fetch("a")), 60),
        used(tool_turn(fetch("b")), 60),  # takes the run to 120
        answer("later", 10),
    ]
    agent = Agent(tool_context, script)

    response = await agent.run(message="read a and b")

    assert response.status == "error"
    assert response.error is not None
    assert response.error.code == "budget_exceeded"
    assert response.error.message == (
        "the run used 120 tokens; MAX_TOKENS_PER_RUN is 100"
    )
    # The first call is kept; the calls of the reply that crossed are not run.
    assert [call.name for call in response.tool_calls] == ["fetch_site"]
    assert agent.fetched == ["https://example.com/a"]
    assert len(agent.model.calls) == 2

    # The thread stays usable, with a fresh run budget.
    later = await agent.run(thread_id=response.thread_id, message="hello")
    assert later.status == "ok"
    assert later.answer == "later"


@pytest.mark.spec("SPEC-0012", "B-0014", "AT-0007")
async def test_thread_token_budget(make_client: MakeClient) -> None:
    script = [
        answer("one", 90),
        answer("two", 90),  # crosses the thread budget, but is still returned
        answer("the summary", 5),
        answer("four", 90),
    ]
    client, agent = make_client(script)
    first = (await client.post(RUN, json={"message": "one"})).json()
    thread_id = first["thread_id"]
    body = {"thread_id": thread_id, "message": "two"}
    second = (await client.post(RUN, json=body)).json()
    assert second["status"] == "ok"
    assert second["answer"] == "two"

    body = {"thread_id": thread_id, "message": "three"}
    blocked = (await client.post(RUN, json=body)).json()

    assert blocked["status"] == "error"
    assert blocked["error"]["code"] == "budget_exceeded"
    assert "MAX_TOKENS_PER_THREAD is 150" in blocked["error"]["message"]
    assert "Compact or delete the thread" in blocked["error"]["message"]
    assert len(agent.model.calls) == 2  # no model call

    # Compaction is never refused by the budget, and resets the count to the
    # summary's 5 tokens: 5 + 90 is under 150.
    compact = f"/v1/threads/{thread_id}/compact"
    response = await client.post(compact, json={"keep_turns": 0})
    assert response.status_code == 200, response.text

    body = {"thread_id": thread_id, "message": "four"}
    after = (await client.post(RUN, json=body)).json()
    assert after["status"] == "ok"
    assert after["answer"] == "four"


@pytest.mark.spec("SPEC-0012", "B-0009")
async def test_confirmation_gets_fresh_run_budget(tool_context: ToolContext) -> None:
    script = [
        used(tool_turn(note("milk")), 99),
        used(tool_turn(fetch("a")), 2),  # would take an unreset run to 101
        answer("done", 2),
    ]
    agent = Agent(tool_context, script)
    paused = await agent.run(message="note milk, then read a")
    assert paused.status == "awaiting_confirmation"

    response = await agent.run(thread_id=paused.thread_id, confirm=True)

    assert response.status == "ok"
    assert [call.name for call in response.tool_calls] == ["add_note", "fetch_site"]


@pytest.mark.spec("SPEC-0012", "B-0008")
def test_token_estimate() -> None:
    prompt = [SystemMessage(content="x" * 40)]
    reply = AIMessage(
        content="y" * 8,
        tool_calls=[{"name": "web_search", "args": {"q": "z"}, "id": "1"}],
    )

    # 40 + 8 + len('{"q": "z"}') = 58 characters
    estimate = call_tokens(prompt, reply)
    assert (estimate.count, estimate.estimated) == (14, True)
    assert (estimate.input, estimate.output) == (10, 4)

    usage = UsageMetadata(input_tokens=30, output_tokens=7, total_tokens=37)
    reported = reply.model_copy(update={"usage_metadata": usage})
    exact = call_tokens(prompt, reported)
    assert (exact.count, exact.estimated) == (37, False)
    assert (exact.input, exact.output) == (30, 7)


@pytest.mark.spec("SPEC-0012", "B-0008")
async def test_tokens_logged(
    tool_context: ToolContext, capture: Capture, caplog: pytest.LogCaptureFixture
) -> None:
    capture("info")
    agent = Agent(tool_context, [answer("hi", 12), AIMessage(content="estimated")])
    first = await agent.run(message="hello")
    await agent.run(thread_id=first.thread_id, message="again")

    replies = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(ROOT)
        and record.getMessage().startswith("model.reply")
    ]
    assert "tokens=12" in replies[0]
    assert "estimated=false" in replies[0]
    assert "estimated=true" in replies[1]


# --- Daily cost limit (B-0010, B-0011) -----------------------------------------

DAY = datetime(2000, 1, 1, 23, 59, 30, tzinfo=UTC)  # 30 s before 00:00 UTC
DAY_KEY = "harnessd:spend:2000-01-01"
LIMIT_MESSAGE = "the daily cost limit of $0.01 was reached; it resets at 00:00 UTC"


class MemorySpendStore:
    def __init__(self, totals: dict[str, int] | None = None) -> None:
        self.totals = totals or {}

    async def total(self, key: str) -> int:
        return self.totals.get(key, 0)

    async def add(self, key: str, micro_usd: int) -> int:
        self.totals[key] = self.totals.get(key, 0) + micro_usd
        return self.totals[key]


class CountingGuard(LocalThreadGuard):
    """Counts the locks taken."""

    def __init__(self) -> None:
        super().__init__()
        self.holds = 0

    @asynccontextmanager
    async def hold(self, thread_id: str) -> AsyncGenerator[None]:
        self.holds += 1
        async with super().hold(thread_id):
            yield


@pytest.fixture
def priced(tool_context: ToolContext) -> ToolContext:
    """Gemini Flash-Lite ($0.30 / $2.50 per million) with a $0.01 daily limit
    (10000 micro-dollars) and token budgets out of the way."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="google",
        llm_model="gemini-3.5-flash-lite",
        google_api_key=SecretStr("test-key"),
        daily_cost_limit_usd=0.01,
        notes_path=tool_context.settings.notes_path,
    )
    return dataclasses.replace(tool_context, settings=settings)


def daily(ctx: ToolContext, store: SpendStore) -> DailySpend:
    return DailySpend(store, ctx.settings, now=lambda: DAY)


@pytest.mark.spec("SPEC-0012", "B-0010")
def test_prices() -> None:
    assert price_of("ollama", "anything") == FREE
    assert price_of("anthropic", "claude-haiku-5-5") is None
    flash = price_of("google", "gemini-3.5-flash-lite")
    assert flash is not None
    # 1000 x $0.30 + 1000 x $2.50 per million = $0.0028
    assert cost_micro_usd(flash, 1000, 1000) == 2800
    assert cost_micro_usd(flash, 10, 0) == 3  # 3.0000000000000004 is not 4
    assert cost_micro_usd(flash, 1, 0) == 1  # $0.0000003 rounds up


@pytest.mark.redis
@pytest.mark.spec("SPEC-0012", "B-0010", "AT-0008")
async def test_daily_cost_limit(
    make_client: MakeClient, priced: ToolContext, redis_url: str
) -> None:
    redis = Redis.from_url(redis_url)
    try:
        await redis.set(DAY_KEY, 10_000)  # at the limit
        spend = daily(priced, RedisSpendStore(redis))
        guard = CountingGuard()
        client, agent = make_client([], ctx=priced, spend=spend, guard=guard)

        response = await client.post(RUN, json={"message": "hello"})

        assert response.status_code == 429
        assert response.json() == {
            "error": {"code": "budget_exceeded", "message": LIMIT_MESSAGE}
        }
        assert response.headers["Retry-After"] == "30"
        assert guard.holds == 0  # refused before the lock
        assert agent.model.calls == []

        compact = f"/v1/threads/{uuid.uuid4()}/compact"
        assert (await client.post(compact)).status_code == 429
        assert guard.holds == 0

        # Spend is added in whole micro-dollars and the day's key expires.
        await redis.delete(DAY_KEY)
        flash = Tokens(input=1000, output=1000, estimated=False)
        assert await spend.add("gemini-3.5-flash-lite", flash) is None
        assert int(await redis.get(DAY_KEY) or 0) == 2800
        assert 0 < await redis.ttl(DAY_KEY) <= 48 * 3600
    finally:
        await redis.delete(DAY_KEY)
        await redis.aclose()


@pytest.mark.spec("SPEC-0012", "B-0010")
async def test_daily_limit_ends_run_in_progress(priced: ToolContext) -> None:
    # 9000 spent; a reply of 1000 output tokens adds 2500 and reaches the limit.
    store = MemorySpendStore({DAY_KEY: 9_000})
    script = [used(tool_turn(fetch("a")), 1000), answer("never", 1)]
    agent = Agent(priced, script, spend=daily(priced, store))

    response = await agent.run(message="read a")

    assert response.status == "error"
    assert response.error is not None
    assert response.error.code == "budget_exceeded"
    assert response.error.message == LIMIT_MESSAGE
    assert response.tool_calls == []  # the crossing reply's calls are not run
    assert agent.fetched == []
    assert store.totals[DAY_KEY] == 11_500

    # The next request is refused before the lock.
    with pytest.raises(DailyLimitError) as refused:
        await agent.run(thread_id=response.thread_id, message="again")
    assert refused.value.retry_after == 30
    assert len(agent.model.calls) == 1


@pytest.mark.spec("SPEC-0012", "B-0010")
async def test_daily_limit_keeps_crossing_answer(priced: ToolContext) -> None:
    store = MemorySpendStore({DAY_KEY: 9_000})
    agent = Agent(priced, [answer("done", 1000)], spend=daily(priced, store))

    response = await agent.run(message="hi")

    assert response.status == "ok"
    assert response.answer == "done"
    assert store.totals[DAY_KEY] == 11_500


@pytest.mark.spec("SPEC-0012", "B-0010")
async def test_compaction_and_screen_spend(
    make_client: MakeClient, priced: ToolContext
) -> None:
    store = MemorySpendStore()
    spend = daily(priced, store)
    script = [answer("one", 0), answer("two", 0), answer("the summary", 1000)]
    client, _ = make_client(script, ctx=priced, spend=spend)
    first = (await client.post(RUN, json={"message": "one"})).json()
    body = {"thread_id": first["thread_id"], "message": "two"}
    await client.post(RUN, json=body)

    compact = f"/v1/threads/{first['thread_id']}/compact"
    assert (await client.post(compact, json={"keep_turns": 0})).status_code == 200
    assert store.totals[DAY_KEY] == 2500  # the summary's 1000 output tokens

    reply = used(AIMessage(content='{"probability": 0.1}'), 400)
    screen = ChatScreen(
        ScriptedChatModel(replies=[reply]),
        spend=spend,
        model_name="gemini-3.5-flash-lite",
    )
    assert await screen.probability("page text") == 0.1
    assert store.totals[DAY_KEY] == 3500  # + 400 x $2.50 per million


@pytest.mark.spec("SPEC-0012", "B-0010")
def test_retry_after() -> None:
    settings = Settings(_env_file=None, daily_cost_limit_usd=1)  # type: ignore[call-arg]
    store = MemorySpendStore()
    at = [datetime(2000, 1, 1, 0, 0, tzinfo=UTC)]
    spend = DailySpend(store, settings, now=lambda: at[0])
    assert spend.retry_after() == 24 * 3600
    at[0] = datetime(2000, 1, 1, 23, 59, 59, 500_000, tzinfo=UTC)
    assert spend.retry_after() == 1
