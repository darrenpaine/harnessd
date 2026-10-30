"""The agent loop: model turns, tool calls and confirmations (SPEC-0001)."""

import logging
import time
from collections.abc import Sequence
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.language_models import BaseChatModel
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

from agent.tool_calls import ToolCalls, Tools, tool_message
from logs import elapsed_ms, event, get_logger
from prompts import SYSTEM_PROMPT
from schemas import ErrorCode, ToolError
from tools.base import Tool, ToolContext
from tracing import confirm_span

NOT_RUN = ToolError(code="internal", message="not run: the earlier run ended first")

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
    trace: list[dict[str, Any]]  # ToolCallRecord dumps, in call order
    outcome: Outcome | None


class RunInput(RunCounters):
    """What a new message starts a run with; it never carries the summary."""

    messages: Annotated[list[AnyMessage], add_messages]
    # Carried across a confirmation pause:
    queue: list[ToolCall]  # calls from the latest model turn not yet run
    invalid_turns: int  # model turns in a row with an invalid call


class AgentState(RunInput):
    summary: NotRequired[str | None]  # written by compaction (SPEC-0009 B-0004)


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


def answer_dangling_calls(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """Add a "not run" result for tool calls nobody answered (SPEC-0001 B-0007).

    A truncated or timed-out run leaves calls without results, which providers
    reject. The fix is applied to the prompt only; the saved history is unchanged.
    """
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    repaired: list[BaseMessage] = []
    missing: list[str] = []
    for message in messages:
        if missing and not isinstance(message, ToolMessage):
            repaired.extend(tool_message(NOT_RUN, call_id) for call_id in missing)
            missing = []
        repaired.append(message)
        if isinstance(message, AIMessage):
            missing = [c for c in _call_ids(message) if c not in answered]
    repaired.extend(tool_message(NOT_RUN, call_id) for call_id in missing)
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
        *answer_dangling_calls(state["messages"]),
    ]


def _call_ids(message: AIMessage) -> list[str]:
    calls = [*message.tool_calls, *message.invalid_tool_calls]
    return [call["id"] for call in calls if call["id"]]


def _step(node: str) -> None:
    event(log, logging.INFO, "graph.step", node=node)


def build_graph(
    model: BaseChatModel,
    ctx: ToolContext,
    tools: Tools,
    checkpointer: BaseCheckpointSaver[Any],
) -> AgentGraph:
    """Compile the agent loop with its checkpointer."""
    bound = model.bind_tools([tool_schema(tool) for tool in tools.values()])
    calls = ToolCalls(tools, ctx)

    async def call_model(state: AgentState) -> Command[str]:
        _step("model")
        started = time.perf_counter()
        reply = await bound.ainvoke(build_prompt(state))
        assert isinstance(reply, AIMessage)
        text = str(reply.text).strip()
        requested = len(reply.tool_calls) + len(reply.invalid_tool_calls)
        event(
            log,
            logging.INFO,
            "model.reply",
            tool_calls=requested,
            ms=elapsed_ms(started),
        )
        update: dict[str, Any] = {"messages": [reply]}

        if not requested:
            if text:
                return Command(update=update, goto=END)
            update["outcome"] = Outcome(
                status="error",
                code="model_output_invalid",
                message="the model gave no answer",
            )
            return Command(update=update, goto=END)

        invalid = bool(reply.invalid_tool_calls) or any(
            calls.check(call) is not None for call in reply.tool_calls
        )
        if invalid and state["invalid_turns"] >= 1:
            update["outcome"] = Outcome(
                status="error",
                code="model_output_invalid",
                message="the model sent invalid tool calls twice in a row",
            )
            return Command(update=update, goto=END)

        if state["calls_used"] + requested > state["max_tool_calls"]:
            update["outcome"] = Outcome(status="truncated", answer=text or None)
            return Command(update=update, goto=END)

        update |= {
            "messages": [reply, *calls.malformed(reply)],
            "queue": reply.tool_calls,
            "calls_used": state["calls_used"] + requested,
            "invalid_turns": state["invalid_turns"] + 1 if invalid else 0,
        }
        return Command(update=update, goto="tools")

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
        """Pause for a human decision, then run or reject the call (B-0005)."""
        _step("confirm")
        call, *rest = state["queue"]
        pending = calls.pending_action(call)
        with confirm_span(pending.tool) as span:
            try:
                approved = interrupt(pending.model_dump(mode="json"))
            except GraphInterrupt:
                span.set_attribute("decision", "awaiting")
                raise
            span.set_attribute(
                "decision", "approved" if approved is True else "rejected"
            )
        settled = await calls.run(call) if approved is True else calls.reject(call)
        trace = list(state["trace"])
        if settled.record is not None:
            trace.append(settled.record)
        update = {"messages": [settled.message], "trace": trace, "queue": rest}
        return Command(update=update, goto="tools" if rest else "model")

    graph = StateGraph(AgentState, input_schema=RunInput)
    graph.add_node("model", call_model, destinations=("tools", END))
    graph.add_node("tools", run_tools, destinations=("model", "confirm"))
    graph.add_node("confirm", confirm, destinations=("model", "tools"))
    graph.add_edge(START, "model")
    return graph.compile(checkpointer=checkpointer)
