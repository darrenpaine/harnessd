"""The agent loop: model turns, tool calls and confirmations (SPEC-0001)."""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.tool import ToolCall
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from agent.budgets import BudgetStop, over_budget
from agent.metered import MeteredModel
from agent.screen import Screen
from agent.tool_calls import ToolCalls, Tools, tool_message
from logs import elapsed_ms, event, get_logger
from prompts import SYSTEM_PROMPT
from schemas import ErrorCode, ToolError
from tools.base import Tool, ToolContext
from tracing import confirm_span

NOT_RUN = ToolError(code="internal", message="not run: the earlier run ended first")
# A confirmed call whose run stopped while its tool ran (SPEC-0001 B-0007).
INTERRUPTED = ToolError(
    code="internal",
    message="interrupted: it may have run; check before trying it again",
)

log = get_logger("graph")


class Outcome(TypedDict):
    """Why a run stopped early: truncated, or an error (SPEC-0001 B-0002, B-0004)."""

    status: Literal["truncated", "error"]
    answer: NotRequired[str | None]
    code: NotRequired[ErrorCode]
    message: NotRequired[str]


class RunCounters(TypedDict):
    """The state reset at the start of every request (AgentThreads)."""

    max_tool_calls: int
    calls_used: int
    run_tokens: int  # SPEC-0012 B-0009
    run_estimated: bool  # any call's tokens estimated (B-0008)
    run_cost: int  # micro-USD from the price table; 0 when unpriced (B-0013)
    trace: list[dict[str, Any]]  # ToolCallRecord dumps, in call order
    outcome: Outcome | None


def fresh_counters(max_tool_calls: int) -> RunCounters:
    """The counters a request starts with."""
    return RunCounters(
        max_tool_calls=max_tool_calls,
        calls_used=0,
        run_tokens=0,
        run_estimated=False,
        run_cost=0,
        trace=[],
        outcome=None,
    )


class Decision(TypedDict):
    """What a confirmation resumes the graph with. The request's counter reset
    travels here and the `confirm` step applies it: sent as `Command(update=…)`
    it would be saved before the step runs, and left behind if the step never
    finished (SPEC-0004 B-0007)."""

    approved: bool
    max_tool_calls: int


class RunInput(RunCounters):
    """What a new message starts a run with; it never carries the summary."""

    messages: Annotated[list[AnyMessage], add_messages]
    # Carried across a confirmation pause:
    queue: list[ToolCall]  # calls from the latest model turn not yet run
    invalid_turns: int  # model turns in a row with an invalid call


class AgentState(RunInput):
    summary: NotRequired[str | None]  # written by compaction (SPEC-0009 B-0004)
    # Tokens since the last compaction (SPEC-0012 B-0014); absent counts as 0.
    thread_tokens: NotRequired[int]
    # Ids of approved calls whose result is not saved yet. One still here on a
    # later run was interrupted while its tool ran (SPEC-0001 B-0007).
    confirmed: NotRequired[list[str]]


AgentGraph = CompiledStateGraph[AgentState, None, RunInput]


def tool_schema(tool: Tool[Any, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": _portable(tool.input_model.model_json_schema()),
        },
    }


def _portable(schema: Any) -> Any:
    """Drop `additionalProperties`, which Gemini does not accept (SPEC-0001 B-0001).

    Unknown fields are still rejected: every call is checked against the
    tool's Input model before it runs.
    """
    if isinstance(schema, dict):
        return {
            key: _portable(value)
            for key, value in schema.items()
            if key != "additionalProperties"
        }
    if isinstance(schema, list):
        return [_portable(item) for item in schema]
    return schema


def answer_dangling_calls(
    messages: Sequence[BaseMessage], confirmed: Sequence[str] = ()
) -> list[BaseMessage]:
    """Add a result for tool calls nobody answered (SPEC-0001 B-0007): "not
    run", or "interrupted" for a call in `confirmed`, which may have run.

    A truncated or timed-out run leaves calls without results, which providers
    reject. The fix is applied to the prompt only; the saved history is unchanged.
    """

    def result(call_id: str) -> BaseMessage:
        error = INTERRUPTED if call_id in confirmed else NOT_RUN
        return tool_message(error, call_id)

    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    repaired: list[BaseMessage] = []
    missing: list[str] = []
    for message in messages:
        if missing and not isinstance(message, ToolMessage):
            repaired.extend(result(call_id) for call_id in missing)
            missing = []
        repaired.append(message)
        if isinstance(message, AIMessage):
            missing = [c for c in _call_ids(message) if c not in answered]
    repaired.extend(result(call_id) for call_id in missing)
    return repaired


def build_prompt(state: AgentState) -> list[BaseMessage]:
    """What the model is sent: the system prompt, then the thread's history with
    dangling calls answered. The prompt is built here only, so later specs add to
    the system prompt in one place.

    A compacted thread's summary is appended after the full system prompt,
    never in a user or assistant message, so the history stays valid for every
    provider (SPEC-0009 B-0005). Only the summary is stored with the thread; the
    system prompt comes from the code on every call and is never summarised.
    """
    system = SYSTEM_PROMPT
    if summary := state.get("summary"):
        system += f"\n\nSummary of the earlier conversation: {summary}"
    return [
        SystemMessage(content=system),
        *answer_dangling_calls(state["messages"], state.get("confirmed", ())),
    ]


def _ids(call: ToolCall) -> list[str]:
    return [call["id"]] if call["id"] else []


def _call_ids(message: AIMessage) -> list[str]:
    calls = [*message.tool_calls, *message.invalid_tool_calls]
    return [call["id"] for call in calls if call["id"]]


@dataclass(frozen=True)
class Answer:
    """The model answered; the run ends ok."""


@dataclass(frozen=True)
class Stop:
    """The run ends early with this outcome."""

    outcome: Outcome


@dataclass(frozen=True)
class OverBudget:
    """The reply took a budget to its limit; its tool calls are not run."""

    stop: BudgetStop


@dataclass(frozen=True)
class RunCalls:
    """Queue the turn's calls; `invalid` if any can't run."""

    invalid: bool


Verdict = Answer | Stop | OverBudget | RunCalls


def model_turn(
    reply: AIMessage,
    state: AgentState,
    calls: ToolCalls,
    crossed: BudgetStop | None = None,
) -> Verdict:
    """What a model turn leads to (SPEC-0001 B-0002, B-0004, SPEC-0012 B-0009,
    B-0010, B-0014). `crossed` is the budget the reply took to its limit, if
    any. The checks run in this order: no calls (an answer is returned even if
    it crossed a budget), invalid calls twice in a row, over the cap, a budget
    crossed."""
    requested = len(reply.tool_calls) + len(reply.invalid_tool_calls)
    if not requested:
        if str(reply.text).strip():
            return Answer()
        return Stop(
            Outcome(
                status="error",
                code="model_output_invalid",
                message="the model gave no answer",
            )
        )

    invalid = bool(reply.invalid_tool_calls) or any(
        calls.check(call) is not None for call in reply.tool_calls
    )
    if invalid and state["invalid_turns"] >= 1:
        return Stop(
            Outcome(
                status="error",
                code="model_output_invalid",
                message="the model sent invalid tool calls twice in a row",
            )
        )

    if state["calls_used"] + requested > state["max_tool_calls"]:
        answer = str(reply.text).strip() or None
        return Stop(Outcome(status="truncated", answer=answer))

    if crossed is not None:
        return OverBudget(crossed)

    return RunCalls(invalid=invalid)


def _budget_outcome(stop: BudgetStop) -> Outcome:
    """The run's outcome on a budget stop, which is logged (SPEC-0012 B-0013)."""
    stop.log()
    return Outcome(status="error", code="budget_exceeded", message=stop.message)


def _step(node: str) -> None:
    event(log, logging.INFO, "graph.step", node=node)


def build_graph(
    model: MeteredModel,
    ctx: ToolContext,
    tools: Tools,
    checkpointer: BaseCheckpointSaver[Any],
    *,
    screen: Screen | None = None,
) -> AgentGraph:
    """Compile the agent loop with its checkpointer. `model` counts and prices
    each call and holds the daily cost limit (SPEC-0012 B-0010); `screen` is the
    injection screen, or None when it is off (B-0005)."""
    bound = model.with_tools([tool_schema(tool) for tool in tools.values()])
    calls = ToolCalls(tools, ctx, screen)
    max_run = ctx.settings.max_tokens_per_run
    max_thread = ctx.settings.max_tokens_per_thread

    async def call_model(state: AgentState) -> Command[str]:
        """One model turn, unless a token budget or the daily cost limit is used
        up (SPEC-0012 B-0009, B-0010, B-0014). A reply that crosses one still
        counts: its answer is returned, but its tool calls are not run."""
        _step("model")
        run_tokens = state["run_tokens"]
        thread_tokens = state.get("thread_tokens", 0)
        if stop := over_budget(run_tokens, thread_tokens, max_run, max_thread):
            return Command(update={"outcome": _budget_outcome(stop)}, goto=END)
        if stop := await bound.day_stop():
            return Command(update={"outcome": _budget_outcome(stop)}, goto=END)
        started = time.perf_counter()
        result = await bound.call(build_prompt(state))
        reply, tokens = result.reply, result.tokens
        counts = {
            "run_tokens": run_tokens + tokens.count,
            "thread_tokens": thread_tokens + tokens.count,
        }
        usage = {
            "run_estimated": state["run_estimated"] or tokens.estimated,
            "run_cost": state["run_cost"] + result.cost_micro_usd,
        }
        requested = len(reply.tool_calls) + len(reply.invalid_tool_calls)
        event(
            log,
            logging.INFO,
            "model.reply",
            tool_calls=requested,
            tokens=tokens.count,
            estimated=tokens.estimated,
            ms=elapsed_ms(started),
        )
        crossed = over_budget(**counts, max_run=max_run, max_thread=max_thread)
        update: dict[str, Any] = {"messages": [reply], **counts, **usage}
        match model_turn(reply, state, calls, crossed or result.day_stop):
            case Answer():
                pass
            case Stop(outcome):
                update["outcome"] = outcome
            case OverBudget(stop):
                update["outcome"] = _budget_outcome(stop)
            case RunCalls(invalid):
                update |= {
                    "messages": [reply, *calls.malformed(reply)],
                    "queue": reply.tool_calls,
                    "calls_used": state["calls_used"] + requested,
                    "invalid_turns": state["invalid_turns"] + 1 if invalid else 0,
                }
                return Command(update=update, goto="tools")
        return Command(update=update, goto=END)

    async def run_tools(state: AgentState) -> Command[str]:
        """Run queued calls in order, stopping before a side effect (B-0005)."""
        _step("tools")
        queue = list(state["queue"])
        messages: list[BaseMessage] = []
        trace = list(state["trace"])
        while queue and not calls.needs_confirmation(queue[0]):
            settled = await calls.run(queue.pop(0))
            messages.append(settled.message)
            if settled.record is not None:
                trace.append(settled.record)
        update = {"messages": messages, "trace": trace, "queue": queue}
        return Command(update=update, goto="confirm" if queue else "model")

    async def confirm(state: AgentState) -> Command[str]:
        """Pause for a human decision, then reject the call or save the approval
        for `act` (B-0005). The approval is saved before the tool runs, so a run
        that stops part way never runs it twice (SPEC-0004 B-0007)."""
        _step("confirm")
        call, *rest = state["queue"]
        pending = calls.pending_action(call)
        with confirm_span(pending.tool) as span:
            try:
                decision: Decision = interrupt(pending.model_dump(mode="json"))
            except GraphInterrupt:
                span.set_attribute("decision", "awaiting")
                raise
            approved = decision["approved"] is True
            span.set_attribute("decision", "approved" if approved else "rejected")
        fresh = fresh_counters(decision["max_tool_calls"])
        if approved:
            confirmed = [*state.get("confirmed", []), *_ids(call)]
            return Command(update={**fresh, "confirmed": confirmed}, goto="act")
        settled = calls.reject(call)
        trace = [settled.record] if settled.record is not None else []
        update = {**fresh, "messages": [settled.message], "trace": trace, "queue": rest}
        return Command(update=update, goto="tools" if rest else "model")

    async def act(state: AgentState) -> Command[str]:
        """Run the call `confirm` approved (B-0005)."""
        _step("act")
        call, *rest = state["queue"]
        settled = await calls.run(call)
        trace = list(state["trace"])
        if settled.record is not None:
            trace.append(settled.record)
        update = {
            "messages": [settled.message],
            "trace": trace,
            "queue": rest,
            "confirmed": [i for i in state.get("confirmed", []) if i != call["id"]],
        }
        return Command(update=update, goto="tools" if rest else "model")

    graph = StateGraph(AgentState, input_schema=RunInput)
    graph.add_node("model", call_model, destinations=("tools", END))
    graph.add_node("tools", run_tools, destinations=("model", "confirm"))
    graph.add_node("confirm", confirm, destinations=("model", "tools", "act"))
    graph.add_node("act", act, destinations=("model", "tools"))
    graph.add_edge(START, "model")
    return graph.compile(checkpointer=checkpointer)
