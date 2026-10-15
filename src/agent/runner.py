"""Run one request through the agent graph (SPEC-0001, SPEC-0002, SPEC-0004).

The runner owns what surrounds the graph: the per-thread lock, the run timeout,
the confirmation rules and turning the final state into an AgentResponse.
"""

import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from agent.graph import AgentState
from agent.persistence import ThreadBusyError
from schemas import AgentRequest, AgentResponse, ErrorInfo, PendingAction
from settings import Settings


class ConfirmationStateError(Exception):
    """`message` sent while a confirmation is pending, or `confirm` with nothing
    pending (SPEC-0002 B-0003)."""


class ThreadGuard(Protocol):
    """One run per thread at a time, and the thread's expiry clock."""

    def hold(self, thread_id: str) -> AbstractAsyncContextManager[None]:
        """Hold the thread for one run, or raise ThreadBusyError."""
        ...

    async def touch(self, thread_id: str) -> None:
        """Reset the thread's expiry after a run."""
        ...


class LocalThreadGuard:
    """In-process guard for graphs with an in-memory checkpointer."""

    def __init__(self) -> None:
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @asynccontextmanager
    async def hold(self, thread_id: str) -> AsyncGenerator[None]:
        lock = self._locks[thread_id]
        if lock.locked():
            raise ThreadBusyError(thread_id)
        async with lock:
            yield

    async def touch(self, thread_id: str) -> None:
        pass  # in-memory state does not expire


class AgentRunner:
    def __init__(
        self,
        graph: CompiledStateGraph[AgentState],
        settings: Settings,
        guard: ThreadGuard,
    ) -> None:
        self.graph = graph
        self.settings = settings
        self.guard = guard

    async def run(self, request: AgentRequest) -> AgentResponse:
        """Run a request to its end. Raises ThreadBusyError and
        ConfirmationStateError; dependency failures propagate."""
        thread_id = request.thread_id or uuid4()
        config: RunnableConfig = {"configurable": {"thread_id": str(thread_id)}}
        async with self.guard.hold(str(thread_id)):
            graph_input = await self._input(request, config)
            try:
                async with asyncio.timeout(self.settings.run_timeout_seconds):
                    values = await self.graph.ainvoke(graph_input, config)
            except TimeoutError:
                return await self._timed_out(thread_id, config)
            finally:
                await self.guard.touch(str(thread_id))
        return _response(thread_id, values)

    async def _input(
        self, request: AgentRequest, config: RunnableConfig
    ) -> Command[Any] | AgentState:
        snapshot = await self.graph.aget_state(config)
        pending = bool(snapshot.interrupts)
        if request.confirm is not None:
            if not pending:
                raise ConfirmationStateError("nothing is awaiting confirmation")
            # The paused call and its queue carry over; the counters are new.
            fresh = {
                "max_tool_calls": request.max_tool_calls,
                "calls_used": 0,
                "trace": [],
                "outcome": None,
            }
            return Command(resume=request.confirm, update=fresh)
        if pending:
            raise ConfirmationStateError(
                "the thread is awaiting confirmation; send confirm instead"
            )
        return AgentState(
            messages=[HumanMessage(content=request.message or "")],
            max_tool_calls=request.max_tool_calls,
            calls_used=0,
            trace=[],
            outcome=None,
            queue=[],
            invalid_turns=0,
        )

    async def _timed_out(
        self, thread_id: UUID, config: RunnableConfig
    ) -> AgentResponse:
        """B-0006: report the calls made before the timeout and an error."""
        snapshot = await self.graph.aget_state(config)
        limit = self.settings.run_timeout_seconds
        return AgentResponse(
            thread_id=thread_id,
            status="error",
            tool_calls=snapshot.values.get("trace", []),
            error=ErrorInfo(
                code="internal",
                message=f"the run took longer than {limit:g} s",
            ),
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
