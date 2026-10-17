"""The agent loop (SPEC-0001), on a scripted model and an in-memory checkpointer."""

import asyncio
import dataclasses
import json
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.graph import (
    SYSTEM_PROMPT,
    AgentState,
    build_prompt,
    run_counters,
    tool_schema,
)
from agent.persistence import ThreadBusyError
from agent_support import PAGE_TEXT, Agent, fetch, tool_turn
from schemas import FetchSiteCall, WebSearchCall
from tools import TOOLS
from tools.base import ToolContext


def error_code(message: ToolMessage) -> str:
    return str(json.loads(str(message.content))["code"])


@pytest.mark.spec("SPEC-0001", "B-0002", "AT-0002")
async def test_single_tool_call_traced(tool_context: ToolContext) -> None:
    agent = Agent(tool_context, [tool_turn(fetch("a")), AIMessage(content="done")])

    response = await agent.run(message="read example.com/a")

    assert response.status == "ok"
    assert response.answer == "done"
    [record] = response.tool_calls
    assert isinstance(record, FetchSiteCall)
    assert str(record.input.url) == "https://example.com/a"
    assert record.output is not None and record.output.text == PAGE_TEXT
    assert agent.fetched == ["https://example.com/a"]
    [result] = agent.tool_results(1)
    assert PAGE_TEXT in str(result.content)


@pytest.mark.spec("SPEC-0001", "B-0002", "AT-0003")
async def test_tool_cap_truncates(tool_context: ToolContext) -> None:
    replies = [tool_turn(fetch("1")), tool_turn(fetch("2"))]
    replies.append(tool_turn(fetch("3"), text="partial answer"))
    agent = Agent(tool_context, replies)

    response = await agent.run(message="keep fetching", max_tool_calls=2)

    assert response.status == "truncated"
    assert response.answer == "partial answer"
    assert len(response.tool_calls) == 2
    assert len(agent.fetched) == 2


@pytest.mark.spec("SPEC-0001", "B-0002")
async def test_batch_over_cap_runs_nothing(tool_context: ToolContext) -> None:
    agent = Agent(tool_context, [tool_turn(fetch("1"), fetch("2"))])

    response = await agent.run(message="fetch two", max_tool_calls=1)

    assert response.status == "truncated"
    assert response.answer is None
    assert response.tool_calls == []
    assert agent.fetched == []


@pytest.mark.spec("SPEC-0001", "B-0003", "AT-0004")
async def test_tool_error_returned_to_model(tool_context: ToolContext) -> None:
    # No TAVILY_API_KEY in tests, so web_search fails with upstream_error.
    search = tool_turn(("web_search", {"query": "news"}))
    agent = Agent(tool_context, [search, AIMessage(content="search is down")])

    response = await agent.run(message="any news?")

    assert response.status == "ok"
    [record] = response.tool_calls
    assert isinstance(record, WebSearchCall)
    assert record.error is not None and record.error.code == "upstream_error"
    [result] = agent.tool_results(1)
    assert result.status == "error" and error_code(result) == "upstream_error"


@pytest.mark.spec("SPEC-0001", "B-0004", "AT-0005")
@pytest.mark.parametrize(
    "bad_turn",
    [
        tool_turn(("fetch_site", {"url": "not a url"})),
        AIMessage(
            content="",
            invalid_tool_calls=[
                {"name": "fetch_site", "args": "{url:", "id": "c1", "error": None}
            ],
        ),
    ],
    ids=["args-fail-input-model", "args-not-json"],
)
async def test_malformed_output_errors(
    tool_context: ToolContext, bad_turn: AIMessage
) -> None:
    agent = Agent(tool_context, [bad_turn, bad_turn])

    response = await agent.run(message="fetch something")

    assert response.status == "error"
    assert response.error is not None
    assert response.error.code == "model_output_invalid"
    assert response.tool_calls == []
    [result] = agent.tool_results(1)
    assert error_code(result) == "invalid_input"


@pytest.mark.spec("SPEC-0001", "B-0004")
@pytest.mark.parametrize("content", ["", "   "])
async def test_reply_without_answer_errors(
    tool_context: ToolContext, content: str
) -> None:
    agent = Agent(tool_context, [AIMessage(content=content)])

    response = await agent.run(message="hello")

    assert response.status == "error"
    assert response.error is not None
    assert response.error.code == "model_output_invalid"


@pytest.mark.spec("SPEC-0001", "B-0004", "AT-0008")
async def test_invalid_call_returned_to_model(tool_context: ToolContext) -> None:
    unknown = tool_turn(("delete_files", {"path": "/"}))
    agent = Agent(tool_context, [unknown, AIMessage(content="I can't do that")])

    response = await agent.run(message="clean up")

    assert response.status == "ok"
    assert response.tool_calls == []
    [result] = agent.tool_results(1)
    assert error_code(result) == "invalid_input"


@pytest.mark.spec("SPEC-0001", "B-0004")
async def test_valid_turn_resets_invalid_count(tool_context: ToolContext) -> None:
    bad = tool_turn(("fetch_site", {"url": "nope"}))
    replies = [bad, tool_turn(fetch()), bad, AIMessage(content="done")]
    agent = Agent(tool_context, replies)

    response = await agent.run(message="go")

    assert response.status == "ok"
    assert len(response.tool_calls) == 1


@pytest.mark.spec("SPEC-0001", "B-0006", "AT-0007")
async def test_run_timeout_errors(tool_context: ToolContext) -> None:
    settings = tool_context.settings.model_copy(update={"run_timeout_seconds": 0.1})
    ctx = dataclasses.replace(tool_context, settings=settings)
    agent = Agent(ctx, [AIMessage(content="too late")], delay=1)

    response = await agent.run(message="hello")

    assert response.status == "error"
    assert response.error is not None and response.error.code == "internal"
    assert response.answer is None


@pytest.mark.spec("SPEC-0001", "B-0007")
def test_build_prompt() -> None:
    """System prompt first, then the history, with an unanswered call answered."""
    question = HumanMessage(content="read a")
    turn = tool_turn(fetch("a"))
    state = AgentState(
        messages=[question, turn],
        queue=[],
        invalid_turns=0,
        summary=None,
        **run_counters(5),
    )

    system, *history = build_prompt(state)

    assert isinstance(system, SystemMessage)
    assert system.content == SYSTEM_PROMPT
    assert history[:2] == [question, turn]
    [result] = history[2:]
    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == turn.tool_calls[0]["id"]
    assert error_code(result) == "internal"


@pytest.mark.spec("SPEC-0001", "B-0007", "AT-0010")
async def test_thread_usable_after_truncation(tool_context: ToolContext) -> None:
    replies = [tool_turn(fetch("1"), fetch("2")), AIMessage(content="ok")]
    agent = Agent(tool_context, replies)
    first = await agent.run(message="fetch two", max_tool_calls=1)
    assert first.status == "truncated"

    second = await agent.run(thread_id=first.thread_id, message="never mind")

    assert second.status == "ok"
    prompt = agent.model.calls[1]
    ai_index = next(i for i, m in enumerate(prompt) if isinstance(m, AIMessage))
    results = prompt[ai_index + 1 : ai_index + 3]
    assert all(isinstance(m, ToolMessage) for m in results)
    assert [error_code(m) for m in results if isinstance(m, ToolMessage)] == [
        "internal",
        "internal",
    ]
    assert isinstance(prompt[ai_index + 3], HumanMessage)


@pytest.mark.spec("SPEC-0004", "B-0001")
async def test_thread_continues(tool_context: ToolContext) -> None:
    agent = Agent(tool_context, [AIMessage(content="hi"), AIMessage(content="again")])
    first = await agent.run(message="hello")

    await agent.run(thread_id=first.thread_id, message="still there?")

    contents = [m.content for m in agent.model.calls[1][1:]]
    assert contents == ["hello", "hi", "still there?"]


@pytest.mark.spec("SPEC-0004", "B-0002")
async def test_given_thread_id_is_kept(tool_context: ToolContext) -> None:
    agent = Agent(tool_context, [AIMessage(content="hi")])
    thread_id = uuid.uuid4()

    response = await agent.run(thread_id=thread_id, message="hello")

    assert response.thread_id == thread_id


@pytest.mark.spec("SPEC-0004", "B-0005")
async def test_second_run_on_busy_thread_raises(tool_context: ToolContext) -> None:
    agent = Agent(tool_context, [AIMessage(content="slow")], delay=0.2)
    thread_id = uuid.uuid4()
    first = asyncio.create_task(agent.run(thread_id=thread_id, message="one"))
    await asyncio.sleep(0.05)

    with pytest.raises(ThreadBusyError):
        await agent.run(thread_id=thread_id, message="two")
    assert (await first).status == "ok"


@pytest.mark.spec("SPEC-0001", "B-0001")
@pytest.mark.parametrize("name", sorted(TOOLS))
def test_tool_schema_is_portable(name: str) -> None:
    # Gemini rejects additionalProperties; unknown fields are refused by the
    # Input model instead (see test_extra_args_are_invalid).
    schema = tool_schema(TOOLS[name])
    assert "additionalProperties" not in json.dumps(schema)
    assert schema["function"]["parameters"]["properties"]


@pytest.mark.spec("SPEC-0001", "B-0004")
async def test_extra_args_are_invalid(tool_context: ToolContext) -> None:
    extra = tool_turn(("fetch_site", {"url": "https://example.com/", "depth": 2}))
    agent = Agent(tool_context, [extra, AIMessage(content="ok")])

    response = await agent.run(message="fetch")

    assert response.status == "ok"
    assert response.tool_calls == []
    assert agent.fetched == []
    [result] = agent.tool_results(1)
    assert error_code(result) == "invalid_input"
