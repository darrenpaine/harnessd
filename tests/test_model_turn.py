"""The rules that decide what a model turn leads to, and their order, tested
without running a graph (SPEC-0001 B-0002, B-0004, SPEC-0012 B-0009, B-0014)."""

from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage

from agent.budgets import BudgetStop
from agent.graph import (
    AgentState,
    Answer,
    OverBudget,
    RunCalls,
    Stop,
    Verdict,
    model_turn,
)
from agent.tool_calls import ToolCalls
from agent_support import fetch, tool_turn
from tools import TOOLS
from tools.base import ToolContext

CROSSED = BudgetStop("run", 120, 100, "the run used 120 tokens")


def state(calls_used: int = 0, invalid_turns: int = 0) -> AgentState:
    values: dict[str, Any] = {
        "max_tool_calls": 2,
        "calls_used": calls_used,
        "invalid_turns": invalid_turns,
    }
    return cast(AgentState, values)


def unknown_call() -> tuple[str, dict[str, Any]]:
    return ("no_such_tool", {})


def verdict(
    ctx: ToolContext,
    reply: AIMessage,
    current: AgentState,
    crossed: BudgetStop | None = CROSSED,
) -> Verdict:
    return model_turn(reply, current, ToolCalls(TOOLS, ctx), crossed)


@pytest.mark.spec("SPEC-0012", "B-0009")
def test_crossing_answer_is_returned(tool_context: ToolContext) -> None:
    reply = AIMessage(content="done")

    assert verdict(tool_context, reply, state()) == Answer()


@pytest.mark.spec("SPEC-0012", "B-0009", "B-0014")
def test_crossing_calls_are_not_run(tool_context: ToolContext) -> None:
    reply = tool_turn(fetch())

    assert verdict(tool_context, reply, state()) == OverBudget(CROSSED)
    assert verdict(tool_context, reply, state(), crossed=None) == RunCalls(False)


@pytest.mark.spec("SPEC-0001", "B-0002", "B-0004")
@pytest.mark.parametrize(
    ("reply", "current", "status", "code"),
    [
        # No answer is checked first.
        (AIMessage(content=""), state(), "error", "model_output_invalid"),
        # Then invalid calls twice in a row.
        (
            tool_turn(unknown_call()),
            state(invalid_turns=1),
            "error",
            "model_output_invalid",
        ),
        # Then the tool-call cap.
        (tool_turn(fetch(), fetch()), state(calls_used=1), "truncated", None),
    ],
    ids=["no-answer", "invalid-twice", "over-cap"],
)
def test_stops_come_before_a_crossed_budget(
    tool_context: ToolContext,
    reply: AIMessage,
    current: AgentState,
    status: str,
    code: str | None,
) -> None:
    result = verdict(tool_context, reply, current)

    assert isinstance(result, Stop)
    assert result.outcome["status"] == status
    assert result.outcome.get("code") == code


@pytest.mark.spec("SPEC-0001", "B-0004")
@pytest.mark.spec("SPEC-0012", "B-0009")
def test_first_invalid_turn_over_budget_stops(tool_context: ToolContext) -> None:
    reply = tool_turn(unknown_call())

    assert verdict(tool_context, reply, state()) == OverBudget(CROSSED)
    assert verdict(tool_context, reply, state(), crossed=None) == RunCalls(True)
