"""Confirmation before side-effecting tools (SPEC-0001 B-0005, SPEC-0003 B-0004)."""

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent.agent_threads import ConfirmationStateError
from agent_support import Agent, fetch, note, tool_turn
from schemas import AddNoteCall
from tools.base import ToolContext


def notes(ctx: ToolContext) -> list[str]:
    path: Path = ctx.settings.notes_path
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.spec("SPEC-0001", "B-0005", "AT-0006")
@pytest.mark.parametrize("approve", [True, False])
async def test_add_note_confirm_flow(tool_context: ToolContext, approve: bool) -> None:
    agent = Agent(tool_context, [tool_turn(note("buy milk")), AIMessage(content="ok")])

    paused = await agent.run(message="note: buy milk")

    assert paused.status == "awaiting_confirmation"
    assert paused.pending_action is not None
    assert paused.pending_action.tool == "add_note"
    assert paused.pending_action.input.text == "buy milk"
    assert paused.tool_calls == []
    assert notes(tool_context) == []

    done = await agent.run(thread_id=paused.thread_id, confirm=approve)

    assert done.status == "ok"
    [record] = done.tool_calls
    assert isinstance(record, AddNoteCall)
    if approve:
        assert record.output is not None
        [line] = notes(tool_context)
        assert line.endswith("\tbuy milk")
    else:
        assert record.error is not None and record.error.code == "rejected"
        assert notes(tool_context) == []


@pytest.mark.spec("SPEC-0003", "B-0004", "AT-0004")
async def test_side_effect_runs_once(tool_context: ToolContext) -> None:
    agent = Agent(tool_context, [tool_turn(note("once")), AIMessage(content="ok")])
    paused = await agent.run(message="note: once")
    await agent.run(thread_id=paused.thread_id, confirm=True)

    with pytest.raises(ConfirmationStateError):
        await agent.run(thread_id=paused.thread_id, confirm=True)
    assert len(notes(tool_context)) == 1


@pytest.mark.spec("SPEC-0001", "B-0005", "AT-0009")
async def test_batch_runs_in_order(tool_context: ToolContext) -> None:
    batch = tool_turn(fetch("before"), note("between"), fetch("after"))
    agent = Agent(tool_context, [batch, AIMessage(content="done")])

    paused = await agent.run(message="do all three")

    assert paused.status == "awaiting_confirmation"
    assert [r.name for r in paused.tool_calls] == ["fetch_site"]
    assert agent.fetched == ["https://example.com/before"]

    done = await agent.run(thread_id=paused.thread_id, confirm=True)

    assert done.status == "ok"
    assert [r.name for r in done.tool_calls] == ["add_note", "fetch_site"]
    assert agent.fetched[-1] == "https://example.com/after"
    assert len(notes(tool_context)) == 1
    # The model saw all three results, in call order.
    assert [m.tool_call_id for m in agent.tool_results(1)] == [
        "call-fetch_site-0",
        "call-add_note-1",
        "call-fetch_site-2",
    ]


@pytest.mark.spec("SPEC-0001", "B-0005")
async def test_each_side_effect_confirmed(tool_context: ToolContext) -> None:
    batch = tool_turn(note("one"), note("two"))
    agent = Agent(tool_context, [batch, AIMessage(content="done")])

    first = await agent.run(message="two notes")
    assert first.pending_action is not None
    assert first.pending_action.input.text == "one"

    second = await agent.run(thread_id=first.thread_id, confirm=False)
    assert second.status == "awaiting_confirmation"
    assert second.pending_action is not None
    assert second.pending_action.input.text == "two"

    done = await agent.run(thread_id=first.thread_id, confirm=True)
    assert done.status == "ok"
    assert [line.split("\t")[1] for line in notes(tool_context)] == ["two"]


@pytest.mark.spec("SPEC-0002", "B-0003")
async def test_confirm_state_rules(tool_context: ToolContext) -> None:
    agent = Agent(tool_context, [tool_turn(note("x")), AIMessage(content="hi")])
    paused = await agent.run(message="note x")

    with pytest.raises(ConfirmationStateError):
        await agent.run(thread_id=paused.thread_id, message="something else")

    fresh = Agent(tool_context, [])
    with pytest.raises(ConfirmationStateError):
        await fresh.run(confirm=True)
