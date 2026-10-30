"""AgentThreads: a thread's status, runs, history and deletion, through the
module's own interface (scripted model, in-memory checkpointer; one Redis test).
"""

import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.agent_threads import AgentThreads, ConfirmationStateError
from agent.persistence import open_checkpointer
from agent_support import Agent, fetch, note, tool_turn
from schemas import FetchSiteCall
from tools.base import ToolContext


def threads(agent: Agent) -> AgentThreads:
    return AgentThreads(agent.graph)


@pytest.mark.spec("SPEC-0004", "B-0002")
@pytest.mark.spec("SPEC-0009", "B-0001")
async def test_state_missing_then_idle(tool_context: ToolContext) -> None:
    agent = threads(Agent(tool_context, [AIMessage(content="hi there")]))
    thread_id = uuid.uuid4()
    assert await agent.state(thread_id) is None

    run = await agent.start(thread_id, "hello", 5)
    response = await run.invoke()

    assert response.status == "ok" and response.answer == "hi there"
    state = await agent.state(thread_id)
    assert state is not None
    assert state.status == "idle" and state.pending_action is None
    assert state.summary is None
    assert [type(m) for m in state.messages] == [HumanMessage, AIMessage]


@pytest.mark.spec("SPEC-0001", "B-0005")
async def test_pause_and_resume(tool_context: ToolContext) -> None:
    agent = threads(
        Agent(tool_context, [tool_turn(note("milk")), AIMessage(content="ok")])
    )
    thread_id = uuid.uuid4()

    paused = await (await agent.start(thread_id, "note milk", 5)).invoke()

    assert paused.status == "awaiting_confirmation"
    state = await agent.state(thread_id)
    assert state is not None and state.status == "awaiting_confirmation"
    assert state.pending_action == paused.pending_action

    run = await agent.resume(thread_id, True, 5)
    assert run.pending_action == paused.pending_action
    done = await run.invoke()

    assert done.status == "ok"
    state = await agent.state(thread_id)
    assert state is not None and state.status == "idle"


@pytest.mark.spec("SPEC-0002", "B-0003")
async def test_confirmation_rules(tool_context: ToolContext) -> None:
    agent = threads(
        Agent(tool_context, [tool_turn(note("milk")), AIMessage(content="ok")])
    )
    thread_id = uuid.uuid4()
    with pytest.raises(ConfirmationStateError):
        await agent.resume(thread_id, True, 5)  # unknown thread

    await (await agent.start(thread_id, "note milk", 5)).invoke()

    with pytest.raises(ConfirmationStateError):
        await agent.start(thread_id, "another", 5)
    await (await agent.resume(thread_id, False, 5)).invoke()
    with pytest.raises(ConfirmationStateError):
        await agent.resume(thread_id, True, 5)


@pytest.mark.spec("SPEC-0001", "B-0006", "B-0007")
@pytest.mark.spec("SPEC-0009", "B-0011")
async def test_unfinished_after_timeout(tool_context: ToolContext) -> None:
    agent = Agent(tool_context, [AIMessage(content="late"), AIMessage(content="ok")])
    agent.model.delay = 1
    thread_id = uuid.uuid4()
    run = await threads(agent).start(thread_id, "slow", 5)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await run.invoke()

    state = await threads(agent).state(thread_id)
    assert state is not None and state.status == "unfinished"
    assert await threads(agent).tool_calls(thread_id) == []

    agent.model.delay = 0
    await (await threads(agent).start(thread_id, "again", 5)).invoke()
    state = await threads(agent).state(thread_id)
    assert state is not None and state.status == "idle"


@pytest.mark.spec("SPEC-0001", "B-0006")
async def test_tool_calls(tool_context: ToolContext) -> None:
    agent = threads(
        Agent(tool_context, [tool_turn(fetch("a")), AIMessage(content="read it")])
    )
    thread_id = uuid.uuid4()
    response = await (await agent.start(thread_id, "read a", 5)).invoke()

    [record] = await agent.tool_calls(thread_id)

    assert isinstance(record, FetchSiteCall)
    assert [record] == response.tool_calls


@pytest.mark.spec("SPEC-0009", "B-0004")
async def test_replace_history(tool_context: ToolContext) -> None:
    replies = [AIMessage(content="one"), AIMessage(content="two")]
    agent = threads(Agent(tool_context, replies))
    thread_id = uuid.uuid4()
    for text in ("first", "second"):
        await (await agent.start(thread_id, text, 5)).invoke()
    before = await agent.state(thread_id)
    assert before is not None

    await agent.replace_history(thread_id, before.messages[:2], "the summary")

    after = await agent.state(thread_id)
    assert after is not None
    assert after.summary == "the summary"
    assert after.messages == before.messages[2:]


@pytest.mark.spec("SPEC-0009", "B-0002")
async def test_delete(tool_context: ToolContext) -> None:
    agent = threads(Agent(tool_context, [AIMessage(content="hi")]))
    thread_id = uuid.uuid4()
    assert await agent.delete(thread_id) is False

    await (await agent.start(thread_id, "hello", 5)).invoke()

    assert await agent.delete(thread_id) is True
    assert await agent.state(thread_id) is None


@pytest.mark.redis
@pytest.mark.spec("SPEC-0004", "B-0006")
async def test_resume_on_redis(redis_url: str, tool_context: ToolContext) -> None:
    settings = tool_context.settings.model_copy(update={"redis_url": redis_url})
    replies = [tool_turn(note("milk")), AIMessage(content="ok")]
    thread_id = uuid.uuid4()
    async with open_checkpointer(settings) as saver:
        agent = threads(Agent(tool_context, replies, checkpointer=saver))
        try:
            await (await agent.start(thread_id, "note milk", 5)).invoke()
            done = await (await agent.resume(thread_id, True, 5)).invoke()

            assert done.status == "ok"
            state = await agent.state(thread_id)
            assert state is not None and state.status == "idle"
        finally:
            await agent.delete(thread_id)
