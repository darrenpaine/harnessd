"""A thread's graph state: its status, runs, history and deletion.

AgentThreads is the only module that reads LangGraph's thread state or builds
the graph's input; the runner, the thread service and the eval runner go
through it. It takes no lock, sets no timeout and doesn't log: callers hold the
thread lock (SPEC-0004 B-0005) and decide how long a run may take.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command, StateSnapshot
from pydantic import TypeAdapter

from agent.budgets import RunUsage
from agent.graph import AgentGraph, Decision, RunInput, fresh_counters
from schemas import (
    AgentResponse,
    AgentThreadState,
    AgentThreadStatus,
    ErrorInfo,
    PendingAction,
    ToolCallRecord,
)
from tools.sanitize import strip_invisible

_TRACE: TypeAdapter[list[ToolCallRecord]] = TypeAdapter(list[ToolCallRecord])


class ConfirmationStateError(Exception):
    """`message` sent while a confirmation is pending, or `confirm` with nothing
    pending (SPEC-0002 B-0003)."""


@dataclass(frozen=True)
class RunResult:
    """A run's response, and what its model calls used."""

    response: AgentResponse
    usage: RunUsage


@dataclass(frozen=True)
class PreparedRun:
    """A run the thread's status allows, ready to invoke. `pending_action` is
    the action a resume decides; None when starting from a message. `stripped`
    counts the invisible characters removed from the message (SPEC-0012 B-0015)."""

    thread_id: UUID
    pending_action: PendingAction | None
    graph: AgentGraph = field(repr=False)
    graph_input: Command[Any] | RunInput = field(repr=False)
    stripped: int = 0

    async def invoke(self) -> RunResult:
        """Run the graph until it answers, pauses, stops early or fails."""
        values = await self.graph.ainvoke(self.graph_input, _config(self.thread_id))
        return RunResult(_response(self.thread_id, values), _usage(values))


class AgentThreads:
    def __init__(self, graph: AgentGraph) -> None:
        self.graph = graph

    async def state(self, thread_id: UUID) -> AgentThreadState | None:
        """The thread's latest state; None if it has none (unknown, expired or
        deleted)."""
        snapshot = await self._snapshot(thread_id)
        if not snapshot.values:
            return None
        pending = _pending(snapshot)
        status: AgentThreadStatus
        if pending is not None:
            status = "awaiting_confirmation"
        elif snapshot.next:
            status = "unfinished"  # a run stopped part way, e.g. on a timeout
        else:
            status = "idle"
        return AgentThreadState(
            status=status,
            pending_action=pending,
            summary=snapshot.values.get("summary"),
            messages=snapshot.values["messages"],
        )

    async def start(
        self, thread_id: UUID, message: str, max_tool_calls: int
    ) -> PreparedRun:
        """A run from a user message. An unknown thread starts empty
        (SPEC-0004 B-0002); an unfinished run is finished by this one
        (SPEC-0001 B-0007). Raises ConfirmationStateError if the thread is
        awaiting confirmation."""
        if _pending(await self._snapshot(thread_id)) is not None:
            raise ConfirmationStateError(
                "the thread is awaiting confirmation; send confirm instead"
            )
        # Only invisible characters are removed; the message is never blocked
        # (SPEC-0012 B-0007, B-0015).
        text, stripped = strip_invisible(message)
        graph_input = RunInput(
            messages=[HumanMessage(content=text)],
            queue=[],
            invalid_turns=0,
            **fresh_counters(max_tool_calls),
        )
        return PreparedRun(thread_id, None, self.graph, graph_input, stripped)

    async def resume(
        self, thread_id: UUID, approved: bool, max_tool_calls: int
    ) -> PreparedRun:
        """A run that approves or rejects the pending action (SPEC-0001 B-0005).
        Raises ConfirmationStateError if nothing is awaiting confirmation."""
        pending = _pending(await self._snapshot(thread_id))
        if pending is None:
            raise ConfirmationStateError("nothing is awaiting confirmation")
        # The paused call and its queue carry over; the `confirm` step resets
        # the counters from the decision.
        decision = Decision(approved=approved, max_tool_calls=max_tool_calls)
        command: Command[Any] = Command(resume=decision)
        return PreparedRun(thread_id, pending, self.graph, command)

    async def tool_calls(self, thread_id: UUID) -> list[ToolCallRecord]:
        """The calls recorded by the thread's latest run, e.g. one that timed out."""
        snapshot = await self._snapshot(thread_id)
        return _TRACE.validate_python(snapshot.values.get("trace", []))

    async def usage(self, thread_id: UUID) -> RunUsage:
        """What the thread's latest run used, e.g. one that timed out."""
        return _usage((await self._snapshot(thread_id)).values)

    async def replace_history(
        self,
        thread_id: UUID,
        removed: Sequence[BaseMessage],
        summary: str,
        summary_tokens: int,
    ) -> None:
        """Remove `removed` from the thread's messages and store `summary` in
        their place (SPEC-0009 B-0004). The thread's token count restarts at the
        summary's size (SPEC-0012 B-0014)."""
        update = {
            "messages": [RemoveMessage(id=m.id or "") for m in removed],
            "summary": summary,
            "thread_tokens": summary_tokens,
        }
        await self.graph.aupdate_state(_config(thread_id), update)

    async def delete(self, thread_id: UUID) -> bool:
        """Remove every checkpoint of the thread; False if it had none. It
        checks for a checkpoint without loading the state, so a thread whose
        state can't be loaded can still be deleted (SPEC-0009 B-0002)."""
        saver = self.graph.checkpointer
        assert isinstance(saver, BaseCheckpointSaver)
        if await saver.aget_tuple(_config(thread_id)) is None:
            return False
        await saver.adelete_thread(str(thread_id))
        return True

    async def _snapshot(self, thread_id: UUID) -> StateSnapshot:
        return await self.graph.aget_state(_config(thread_id))


def _config(thread_id: UUID) -> RunnableConfig:
    """Selects a thread's checkpoint, so runs and reads see the same state."""
    return {"configurable": {"thread_id": str(thread_id)}}


def _pending(snapshot: StateSnapshot) -> PendingAction | None:
    if not snapshot.interrupts:
        return None
    return PendingAction.model_validate(snapshot.interrupts[0].value)


def _usage(values: dict[str, Any]) -> RunUsage:
    return RunUsage(
        tokens=values.get("run_tokens", 0),
        estimated=values.get("run_estimated", False),
        cost_micro_usd=values.get("run_cost", 0),
    )


def _response(thread_id: UUID, values: dict[str, Any]) -> AgentResponse:
    trace = values.get("trace", [])
    if interrupts := values.get("__interrupt__"):
        return AgentResponse(
            thread_id=thread_id,
            status="awaiting_confirmation",
            tool_calls=trace,
            pending_action=PendingAction.model_validate(interrupts[0].value),
        )
    outcome = values.get("outcome")
    if outcome is None:
        reply = values["messages"][-1]
        assert isinstance(reply, AIMessage)
        return AgentResponse(
            thread_id=thread_id,
            status="ok",
            answer=str(reply.text).strip(),
            tool_calls=trace,
        )
    if outcome["status"] == "truncated":
        return AgentResponse(
            thread_id=thread_id,
            status="truncated",
            answer=outcome.get("answer"),
            tool_calls=trace,
        )
    return AgentResponse(
        thread_id=thread_id,
        status="error",
        tool_calls=trace,
        error=ErrorInfo(code=outcome["code"], message=outcome["message"]),
    )
