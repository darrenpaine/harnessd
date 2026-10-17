"""The agent loop: model turns, tool calls and confirmations (SPEC-0001)."""

import logging
import time
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    InvalidToolCall,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.tool import ToolCall
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, TypeAdapter, ValidationError

from logs import clip, elapsed_ms, event, get_logger
from schemas import ErrorCode, PendingAction, ToolCallRecord, ToolError
from tools.base import Tool, ToolContext, call_tool, summarize

SYSTEM_PROMPT = (
    "You are a helpful assistant. Use the tools when they help answer the user. "
    "When a tool returns an error, decide whether to retry, try another tool, or "
    "answer without it."
)

NOT_RUN = ToolError(code="internal", message="not run: the earlier run ended first")
MALFORMED = ToolError(code="invalid_input", message="arguments are not valid JSON")

log = get_logger("graph")

_RECORD: TypeAdapter[ToolCallRecord] = TypeAdapter(ToolCallRecord)


class Outcome(TypedDict):
    """Why a run stopped early: truncated, or an error (SPEC-0001 B-0002, B-0004)."""

    status: Literal["truncated", "error"]
    answer: NotRequired[str | None]
    code: NotRequired[ErrorCode]
    message: NotRequired[str]


class RunCounters(TypedDict):
    """The state reset at the start of every request; see `run_counters`."""

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


def run_counters(max_tool_calls: int) -> RunCounters:
    return RunCounters(
        max_tool_calls=max_tool_calls, calls_used=0, trace=[], outcome=None
    )


Tools = Mapping[str, Tool[Any, Any]]


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
            repaired.extend(_tool_message(NOT_RUN, call_id) for call_id in missing)
            missing = []
        repaired.append(message)
        if isinstance(message, AIMessage):
            missing = [c for c in _call_ids(message) if c not in answered]
    repaired.extend(_tool_message(NOT_RUN, call_id) for call_id in missing)
    return repaired


def build_prompt(state: AgentState) -> list[BaseMessage]:
    """What the model is sent: the system prompt, then the thread's history with
    dangling calls answered. The prompt is built here only, so later specs add to
    the system prompt in one place.

    A compacted thread's summary goes in the system prompt, never in a user or
    assistant message, so the history stays valid for every provider
    (SPEC-0009 B-0005).
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


def _tool_message(result: BaseModel, call_id: str) -> ToolMessage:
    return ToolMessage(
        content=result.model_dump_json(),
        tool_call_id=call_id,
        status="error" if isinstance(result, ToolError) else "success",
    )


def _parse(call: ToolCall, tools: Tools) -> BaseModel | ToolError:
    """The call's validated input, or why it can't run (SPEC-0001 B-0004)."""
    tool = tools.get(call["name"])
    if tool is None:
        return ToolError(code="invalid_input", message=f"no tool {call['name']!r}")
    try:
        data: BaseModel = tool.input_model.model_validate(call["args"])
    except ValidationError as exc:
        return ToolError(code="invalid_input", message=summarize(exc))
    return data


def _record(call: ToolCall, data: BaseModel, result: BaseModel) -> dict[str, Any]:
    key = "error" if isinstance(result, ToolError) else "output"
    record = _RECORD.validate_python(
        {"id": call["id"], "name": call["name"], "input": data, key: result}
    )
    return record.model_dump(mode="json")


def _malformed_results(reply: AIMessage) -> list[ToolMessage]:
    """Results for calls whose args weren't valid JSON; they never run."""
    return [
        _tool_message(MALFORMED, call["id"] or "") for call in reply.invalid_tool_calls
    ]


def _step(node: str) -> None:
    event(log, logging.INFO, "graph.step", node=node)


def _log_call(
    call: ToolCall | InvalidToolCall, result: BaseModel, started: float | None
) -> None:
    """tool.call for one call; `started` is None for calls that never ran."""
    event(
        log,
        logging.INFO,
        "tool.call",
        tool=call["name"],
        call_id=call["id"],
        result=result.code if isinstance(result, ToolError) else "ok",
        ms=0 if started is None else elapsed_ms(started),
    )


async def _run_call(
    tool: Tool[Any, Any], call: ToolCall, ctx: ToolContext
) -> BaseModel:
    """Run one validated call, logging its input (debug) and result."""
    event(
        log,
        logging.DEBUG,
        "tool.input",
        tool=call["name"],
        call_id=call["id"],
        input=clip(call["args"]),
    )
    started = time.perf_counter()
    result: BaseModel = await call_tool(tool, call["args"], ctx)
    _log_call(call, result, started)
    return result


def build_graph(
    model: BaseChatModel,
    ctx: ToolContext,
    tools: Tools,
    checkpointer: BaseCheckpointSaver[Any],
) -> AgentGraph:
    """Compile the agent loop with its checkpointer."""
    bound = model.bind_tools([tool_schema(tool) for tool in tools.values()])

    async def call_model(state: AgentState) -> Command[str]:
        _step("model")
        started = time.perf_counter()
        reply = await bound.ainvoke(build_prompt(state))
        assert isinstance(reply, AIMessage)
        text = str(reply.text).strip()
        calls = reply.tool_calls
        malformed = _malformed_results(reply)
        event(
            log,
            logging.INFO,
            "model.reply",
            tool_calls=len(calls) + len(malformed),
            ms=elapsed_ms(started),
        )
        update: dict[str, Any] = {"messages": [reply]}

        if not calls and not malformed:
            if text:
                return Command(update=update, goto=END)
            update["outcome"] = Outcome(
                status="error",
                code="model_output_invalid",
                message="the model gave no answer",
            )
            return Command(update=update, goto=END)

        invalid = bool(malformed) or any(
            isinstance(_parse(call, tools), ToolError) for call in calls
        )
        if invalid and state["invalid_turns"] >= 1:
            update["outcome"] = Outcome(
                status="error",
                code="model_output_invalid",
                message="the model sent invalid tool calls twice in a row",
            )
            return Command(update=update, goto=END)

        requested = len(calls) + len(malformed)
        if state["calls_used"] + requested > state["max_tool_calls"]:
            update["outcome"] = Outcome(status="truncated", answer=text or None)
            return Command(update=update, goto=END)

        for bad in reply.invalid_tool_calls:
            _log_call(bad, MALFORMED, None)
        update |= {
            "messages": [reply, *malformed],
            "queue": calls,
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
        while queue:
            call = queue[0]
            data = _parse(call, tools)
            if isinstance(data, ToolError):
                _log_call(call, data, None)
                messages.append(_tool_message(data, call["id"] or ""))
            elif tools[call["name"]].side_effect:
                break
            else:
                result = await _run_call(tools[call["name"]], call, ctx)
                messages.append(_tool_message(result, call["id"] or ""))
                trace.append(_record(call, data, result))
            queue.pop(0)
        update = {"messages": messages, "trace": trace, "queue": queue}
        return Command(update=update, goto="confirm" if queue else "model")

    async def confirm(state: AgentState) -> Command[str]:
        """Pause for a human decision, then run or reject the call (B-0005)."""
        _step("confirm")
        call, *rest = state["queue"]
        tool = tools[call["name"]]
        data = tool.input_model.model_validate(call["args"])
        pending = PendingAction.model_validate({"tool": tool.name, "input": data})
        approved = interrupt(pending.model_dump(mode="json"))
        result: BaseModel
        if approved is True:
            result = await _run_call(tool, call, ctx)
        else:
            result = ToolError(code="rejected", message="the user declined this call")
            _log_call(call, result, None)
        update = {
            "messages": [_tool_message(result, call["id"] or "")],
            "trace": [*state["trace"], _record(call, data, result)],
            "queue": rest,
        }
        return Command(update=update, goto="tools" if rest else "model")

    graph = StateGraph(AgentState, input_schema=RunInput)
    graph.add_node("model", call_model, destinations=("tools", END))
    graph.add_node("tools", run_tools, destinations=("model", "confirm"))
    graph.add_node("confirm", confirm, destinations=("model", "tools"))
    graph.add_edge(START, "model")
    return graph.compile(checkpointer=checkpointer)
