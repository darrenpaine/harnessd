"""The agent loop: model turns, tool calls and confirmations (SPEC-0001)."""

from collections.abc import Mapping, Sequence
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
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, TypeAdapter, ValidationError

from schemas import PendingAction, ToolCallRecord, ToolError
from tools.base import Tool, ToolContext, call_tool, summarize

SYSTEM_PROMPT = (
    "You are a helpful assistant. Use the tools when they help answer the user. "
    "When a tool returns an error, decide whether to retry, try another tool, or "
    "answer without it."
)

NOT_RUN = ToolError(code="internal", message="not run: the earlier run ended first")

_RECORD: TypeAdapter[ToolCallRecord] = TypeAdapter(ToolCallRecord)


class Outcome(TypedDict):
    """Why a run stopped early: truncated, or an error (SPEC-0001 B-0002, B-0004)."""

    status: Literal["truncated", "error"]
    answer: NotRequired[str | None]
    code: NotRequired[Literal["model_output_invalid"]]
    message: NotRequired[str]


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    # Reset by the runner at the start of every request:
    max_tool_calls: int
    calls_used: int
    trace: list[dict[str, Any]]  # ToolCallRecord dumps, in call order
    outcome: Outcome | None
    # Carried across a confirmation pause:
    queue: list[ToolCall]  # calls from the latest model turn not yet run
    invalid_turns: int  # model turns in a row with an invalid call


Tools = Mapping[str, Tool[Any, Any]]


def tool_schema(tool: Tool[Any, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_model.model_json_schema(),
        },
    }


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
        _tool_message(
            ToolError(code="invalid_input", message="arguments are not valid JSON"),
            call["id"] or "",
        )
        for call in reply.invalid_tool_calls
    ]


def build_graph(
    model: BaseChatModel,
    ctx: ToolContext,
    tools: Tools,
    checkpointer: BaseCheckpointSaver[Any],
) -> CompiledStateGraph[AgentState]:
    """Compile the agent loop with its checkpointer."""
    bound = model.bind_tools([tool_schema(tool) for tool in tools.values()])

    async def call_model(state: AgentState) -> Command[str]:
        prompt = [
            SystemMessage(content=SYSTEM_PROMPT),
            *answer_dangling_calls(state["messages"]),
        ]
        reply = await bound.ainvoke(prompt)
        assert isinstance(reply, AIMessage)
        text = str(reply.text).strip()
        calls = reply.tool_calls
        malformed = _malformed_results(reply)
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

        update |= {
            "messages": [reply, *malformed],
            "queue": calls,
            "calls_used": state["calls_used"] + requested,
            "invalid_turns": state["invalid_turns"] + 1 if invalid else 0,
        }
        return Command(update=update, goto="tools")

    async def run_tools(state: AgentState) -> Command[str]:
        """Run queued calls in order, stopping before a side effect (B-0005)."""
        queue = list(state["queue"])
        messages: list[BaseMessage] = []
        trace = list(state["trace"])
        while queue:
            call = queue[0]
            data = _parse(call, tools)
            if isinstance(data, ToolError):
                messages.append(_tool_message(data, call["id"] or ""))
            elif tools[call["name"]].side_effect:
                break
            else:
                result = await call_tool(tools[call["name"]], call["args"], ctx)
                messages.append(_tool_message(result, call["id"] or ""))
                trace.append(_record(call, data, result))
            queue.pop(0)
        update = {"messages": messages, "trace": trace, "queue": queue}
        return Command(update=update, goto="confirm" if queue else "model")

    async def confirm(state: AgentState) -> Command[str]:
        """Pause for a human decision, then run or reject the call (B-0005)."""
        call, *rest = state["queue"]
        tool = tools[call["name"]]
        data = tool.input_model.model_validate(call["args"])
        pending = PendingAction.model_validate({"tool": tool.name, "input": data})
        approved = interrupt(pending.model_dump(mode="json"))
        result: BaseModel
        if approved is True:
            result = await call_tool(tool, call["args"], ctx)
        else:
            result = ToolError(code="rejected", message="the user declined this call")
        update = {
            "messages": [_tool_message(result, call["id"] or "")],
            "trace": [*state["trace"], _record(call, data, result)],
            "queue": rest,
        }
        return Command(update=update, goto="tools" if rest else "model")

    graph = StateGraph(AgentState)
    graph.add_node("model", call_model, destinations=("tools", END))
    graph.add_node("tools", run_tools, destinations=("model", "confirm"))
    graph.add_node("confirm", confirm, destinations=("model", "tools"))
    graph.add_edge(START, "model")
    return graph.compile(checkpointer=checkpointer)
