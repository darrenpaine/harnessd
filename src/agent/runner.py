"""Run one request through the agent graph (SPEC-0001, SPEC-0002, SPEC-0004).

The runner owns what surrounds the graph: the per-thread lock, the run timeout,
the confirmation rules and turning the final state into an AgentResponse.
"""

import asyncio
import logging
import time
from typing import Any
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from opentelemetry.trace import Span, StatusCode

from agent.graph import AgentGraph, RunInput, run_counters, thread_config
from agent.persistence import ThreadGuard
from logs import elapsed_ms, event, get_logger, log_context
from schemas import AgentRequest, AgentResponse, ErrorInfo, PendingAction
from settings import Settings
from tracing import INPUT, OUTPUT, record_content, thread_span

log = get_logger("runner")


class ConfirmationStateError(Exception):
    """`message` sent while a confirmation is pending, or `confirm` with nothing
    pending (SPEC-0002 B-0003)."""


class AgentRunner:
    def __init__(
        self,
        graph: AgentGraph,
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
        with (
            log_context(thread=str(thread_id)),
            thread_span(
                "agent.run",
                thread_id,
                span_kind="AGENT",
                kind=_kind(request),
                max_tool_calls=request.max_tool_calls,
            ) as span,
        ):
            record_content(span, INPUT, request.message)
            started = time.perf_counter()
            response = await self._run(request, thread_id)
            _log_end(response, started)
            _trace_end(span, response)
            return response

    async def _run(self, request: AgentRequest, thread_id: UUID) -> AgentResponse:
        config = thread_config(thread_id)
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

    def _log_start(self, request: AgentRequest) -> None:
        event(
            log,
            logging.INFO,
            "run.start",
            kind=_kind(request),
            max_tool_calls=request.max_tool_calls,
            provider=self.settings.llm_provider,
            model=self.settings.llm_model,
        )

    async def _input(
        self, request: AgentRequest, config: RunnableConfig
    ) -> Command[Any] | RunInput:
        snapshot = await self.graph.aget_state(config)
        pending = bool(snapshot.interrupts)
        if request.confirm is not None:
            if not pending:
                raise ConfirmationStateError("nothing is awaiting confirmation")
            self._log_start(request)
            tool = snapshot.interrupts[0].value["tool"]
            event(
                log,
                logging.INFO,
                "confirm.decision",
                tool=tool,
                approved=request.confirm,
            )
            # The paused call and its queue carry over; the counters are new.
            fresh = run_counters(request.max_tool_calls)
            return Command(resume=request.confirm, update=fresh)
        if pending:
            raise ConfirmationStateError(
                "the thread is awaiting confirmation; send confirm instead"
            )
        self._log_start(request)
        return RunInput(
            messages=[HumanMessage(content=request.message or "")],
            queue=[],
            invalid_turns=0,
            **run_counters(request.max_tool_calls),
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


def _kind(request: AgentRequest) -> str:
    return "message" if request.message is not None else "confirm"


def _trace_end(span: Span, response: AgentResponse) -> None:
    """The run's outcome on agent.run (SPEC-0010 B-0002)."""
    span.set_attribute("status", response.status)
    span.set_attribute("tool_calls", len(response.tool_calls))
    if response.error is not None:
        span.set_attribute("error", response.error.code)
        span.set_status(StatusCode.ERROR, response.error.message)
    record_content(span, OUTPUT, response.answer)


def _log_end(response: AgentResponse, started: float) -> None:
    """run.end, and confirm.wait when the run paused (SPEC-0008 B-0002)."""
    if response.pending_action is not None:
        event(log, logging.INFO, "confirm.wait", tool=response.pending_action.tool)
    event(
        log,
        logging.INFO,
        "run.end",
        status=response.status,
        tool_calls=len(response.tool_calls),
        ms=elapsed_ms(started),
        error=response.error.code if response.error is not None else None,
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
