"""What the graph does with one tool call: validate, run or refuse, and record
it, with its log event and span (SPEC-0001 B-0003..B-0005)."""

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, InvalidToolCall, ToolMessage
from langchain_core.messages.tool import ToolCall
from pydantic import BaseModel, TypeAdapter, ValidationError

from logs import clip, elapsed_ms, event, get_logger
from schemas import PendingAction, ToolCallRecord, ToolError
from tools.base import Tool, ToolContext, call_tool, summarize
from tracing import tool_result, tool_span

MALFORMED = ToolError(code="invalid_input", message="arguments are not valid JSON")
REJECTED = ToolError(code="rejected", message="the user declined this call")

# tool.call stays on the graph's logger, where it was before this module existed.
log = get_logger("graph")

_RECORD: TypeAdapter[ToolCallRecord] = TypeAdapter(ToolCallRecord)

Tools = Mapping[str, Tool[Any, Any]]


@dataclass(frozen=True)
class Settled:
    """A call's result message, and its trace record (None for invalid input)."""

    message: ToolMessage
    record: dict[str, Any] | None


def tool_message(result: BaseModel, call_id: str | None) -> ToolMessage:
    return ToolMessage(
        content=result.model_dump_json(),
        tool_call_id=call_id or "",
        status="error" if isinstance(result, ToolError) else "success",
    )


class ToolCalls:
    """The graph's tools, and what happens to each call the model makes."""

    def __init__(self, tools: Tools, ctx: ToolContext) -> None:
        self._tools = tools
        self._ctx = ctx

    def check(self, call: ToolCall) -> ToolError | None:
        """Why the call can't run (SPEC-0001 B-0004), or None."""
        parsed = self._parse(call)
        return parsed if isinstance(parsed, ToolError) else None

    def needs_confirmation(self, call: ToolCall) -> bool:
        """A valid call to a tool with a side effect (B-0005)."""
        return self.check(call) is None and self._tools[call["name"]].side_effect

    def pending_action(self, call: ToolCall) -> PendingAction:
        tool = self._tools[call["name"]]
        data = tool.input_model.model_validate(call["args"])
        return PendingAction.model_validate({"tool": tool.name, "input": data})

    async def run(self, call: ToolCall) -> Settled:
        """Run the call, or refuse it when its input is invalid."""
        data = self._parse(call)
        if isinstance(data, ToolError):
            _not_run(call, data)
            return Settled(tool_message(data, call["id"]), None)
        result = await self._run(self._tools[call["name"]], call)
        return Settled(tool_message(result, call["id"]), _record(call, data, result))

    def reject(self, call: ToolCall) -> Settled:
        """The user declined the call (B-0005)."""
        data = self._tools[call["name"]].input_model.model_validate(call["args"])
        _not_run(call, REJECTED)
        return Settled(
            tool_message(REJECTED, call["id"]), _record(call, data, REJECTED)
        )

    def malformed(self, reply: AIMessage) -> list[ToolMessage]:
        """Results for calls whose args weren't valid JSON; they never run."""
        for call in reply.invalid_tool_calls:
            _not_run(call, MALFORMED)
        return [
            tool_message(MALFORMED, call["id"]) for call in reply.invalid_tool_calls
        ]

    def _parse(self, call: ToolCall) -> BaseModel | ToolError:
        tool = self._tools.get(call["name"])
        if tool is None:
            return ToolError(code="invalid_input", message=f"no tool {call['name']!r}")
        try:
            data: BaseModel = tool.input_model.model_validate(call["args"])
        except ValidationError as exc:
            return ToolError(code="invalid_input", message=summarize(exc))
        return data

    async def _run(self, tool: Tool[Any, Any], call: ToolCall) -> BaseModel:
        """Run one validated call, logging its input (debug) and result, in an
        agent.tool span (SPEC-0010)."""
        event(
            log,
            logging.DEBUG,
            "tool.input",
            tool=call["name"],
            call_id=call["id"],
            input=clip(call["args"]),
        )
        with tool_span(call["name"], call["id"], call["args"]) as span:
            started = time.perf_counter()
            result: BaseModel = await call_tool(tool, call["args"], self._ctx)
            tool_result(span, result)
        _log_call(call, result, started)
        return result


def _record(call: ToolCall, data: BaseModel, result: BaseModel) -> dict[str, Any]:
    key = "error" if isinstance(result, ToolError) else "output"
    record = _RECORD.validate_python(
        {"id": call["id"], "name": call["name"], "input": data, key: result}
    )
    return record.model_dump(mode="json")


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


def _not_run(call: ToolCall | InvalidToolCall, result: ToolError) -> None:
    """Log and trace a call that never ran: invalid, malformed or rejected."""
    with tool_span(call["name"], call["id"], call["args"]) as span:
        tool_result(span, result)
    _log_call(call, result, None)
