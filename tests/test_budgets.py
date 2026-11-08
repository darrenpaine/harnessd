"""Token budgets per run and per thread (SPEC-0012 B-0008, B-0009, B-0014).

A scripted model reports the usage of each reply, so the counts are exact.
"""

import dataclasses

import pytest
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.messages.ai import UsageMetadata

from agent.budgets import call_tokens, output_tokens
from agent_support import Agent, Capture, MakeClient, fetch, note, tool_turn
from logs import ROOT
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
    assert output_tokens(reply) == 4

    usage = UsageMetadata(input_tokens=30, output_tokens=7, total_tokens=37)
    reported = reply.model_copy(update={"usage_metadata": usage})
    exact = call_tokens(prompt, reported)
    assert (exact.count, exact.estimated) == (37, False)
    assert output_tokens(reported) == 7


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
