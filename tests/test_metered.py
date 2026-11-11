"""The metered model: every model call is counted, priced and added to the day's
spend in one place (SPEC-0012 B-0008, B-0010, B-0011)."""

from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages.ai import UsageMetadata
from pydantic import SecretStr

from agent.metered import MeteredModel
from agent.spend import DailyLimitError, DailySpend
from agent_support import MemorySpendStore
from fakes import ScriptedChatModel
from settings import Settings

DAY = datetime(2000, 1, 1, 12, 0, tzinfo=UTC)
DAY_KEY = "harnessd:spend:2000-01-01"
PROMPT = [HumanMessage(content="hello")]
TOOL = {"type": "function", "function": {"name": "t", "parameters": {}}}


def flash(daily_cost_limit_usd: float | None = None) -> Settings:
    """Gemini Flash-Lite: $0.30 in, $2.50 out per million tokens."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="google",
        llm_model="gemini-3.5-flash-lite",
        google_api_key=SecretStr("test-key"),
        daily_cost_limit_usd=daily_cost_limit_usd,
    )


def reply(input_tokens: int, output_tokens: int) -> AIMessage:
    usage = UsageMetadata(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )
    return AIMessage(content="hi", usage_metadata=usage)


def limited(store: MemorySpendStore) -> tuple[Settings, DailySpend]:
    """A $0.01 daily limit: 10000 micro-dollars."""
    settings = flash(daily_cost_limit_usd=0.01)
    return settings, DailySpend(store, settings, now=lambda: DAY)


@pytest.mark.spec("SPEC-0012", "B-0008", "B-0010")
async def test_call_is_counted_priced_and_added() -> None:
    store = MemorySpendStore()
    settings, spend = limited(store)
    scripted = ScriptedChatModel(replies=[reply(1000, 1000)])
    model = MeteredModel(scripted, settings, spend)

    result = await model.call(PROMPT)

    assert result.reply.text == "hi"
    assert (result.tokens.input, result.tokens.output) == (1000, 1000)
    assert result.tokens.estimated is False
    assert result.cost_micro_usd == 2800  # 1000 x $0.30 + 1000 x $2.50 per million
    assert result.day_stop is None
    assert store.totals == {DAY_KEY: 2800}
    assert scripted.calls == [PROMPT]


@pytest.mark.spec("SPEC-0012", "B-0008")
async def test_tokens_are_estimated_without_usage() -> None:
    scripted = ScriptedChatModel(replies=[AIMessage(content="y" * 8)])
    model = MeteredModel(scripted, flash())

    result = await model.call([HumanMessage(content="x" * 40)])

    assert (result.tokens.input, result.tokens.output) == (10, 2)
    assert result.tokens.estimated is True


@pytest.mark.spec("SPEC-0012", "B-0010")
async def test_call_that_reaches_the_limit_returns_the_stop() -> None:
    store = MemorySpendStore({DAY_KEY: 9_000})
    settings, spend = limited(store)
    model = MeteredModel(ScriptedChatModel(replies=[reply(0, 1000)]), settings, spend)

    assert await model.day_stop() is None  # 9000 of 10000: a call may be made
    result = await model.call(PROMPT)

    assert result.day_stop is not None
    assert (result.day_stop.scope, result.day_stop.used) == ("day", 0.0115)
    assert await model.day_stop() == result.day_stop
    with pytest.raises(DailyLimitError):
        await model.refuse_if_reached()


@pytest.mark.spec("SPEC-0012", "B-0010")
async def test_tools_bound_model_shares_the_spend() -> None:
    store = MemorySpendStore()
    settings, spend = limited(store)
    scripted = ScriptedChatModel(replies=[reply(0, 1000), reply(0, 1000)])
    model = MeteredModel(scripted, settings, spend)
    bound = model.with_tools([TOOL])

    await model.call(PROMPT)
    result = await bound.call(PROMPT)

    assert result.cost_micro_usd == 2500
    assert store.totals == {DAY_KEY: 5000}
    assert bound.priced and model.priced


@pytest.mark.spec("SPEC-0012", "B-0010", "B-0011")
async def test_without_a_daily_limit_nothing_is_stored_or_stopped() -> None:
    priced = MeteredModel(ScriptedChatModel(replies=[reply(1000, 1000)]), flash())
    result = await priced.call(PROMPT)
    assert (result.cost_micro_usd, result.day_stop) == (2800, None)
    assert await priced.day_stop() is None
    await priced.refuse_if_reached()  # never raises

    # A model with no price costs 0 and is reported as unpriced.
    unpriced = MeteredModel(
        ScriptedChatModel(replies=[reply(1000, 1000)]),
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            llm_provider="anthropic",
            llm_model="claude-haiku-5-5",
            anthropic_api_key=SecretStr("test-key"),
        ),
    )
    assert unpriced.priced is False
    assert (await unpriced.call(PROMPT)).cost_micro_usd == 0


@pytest.mark.spec("SPEC-0012", "B-0011")
async def test_unpriced_model_with_a_daily_limit_is_an_error() -> None:
    """Settings refuse this at startup; the metered model never adds a call it
    can't price."""
    store = MemorySpendStore()
    _, spend = limited(store)
    unpriced = Settings(_env_file=None, llm_provider="ollama")  # type: ignore[call-arg]
    unpriced = unpriced.model_copy(
        update={"llm_provider": "anthropic", "llm_model": "claude-haiku-5-5"}
    )
    model = MeteredModel(ScriptedChatModel(replies=[reply(1, 1)]), unpriced, spend)

    with pytest.raises(ValueError, match="no price for anthropic claude-haiku-5-5"):
        await model.call(PROMPT)
    assert store.totals == {}
