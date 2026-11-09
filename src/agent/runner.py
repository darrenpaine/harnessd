"""Run one request through the agent graph (SPEC-0001, SPEC-0002, SPEC-0004).

The runner owns what surrounds a run: the per-thread lock, the run timeout,
the TTL refresh, secret redaction, logging and tracing. AgentThreads checks the
confirmation rules, runs the graph and turns its result into an AgentResponse.
The run's audit events are logged here (SPEC-0012 B-0013).
"""

import asyncio
import time
from uuid import UUID, uuid4

from opentelemetry.trace import Span, StatusCode

from agent.agent_threads import AgentThreads, PreparedRun, RunResult
from agent.budgets import RunUsage
from agent.graph import AgentGraph
from agent.persistence import ThreadGuard
from agent.prices import MICRO_USD, price_of
from agent.redact import Redactor
from agent.spend import DailySpend
from logs import audit, elapsed_ms, log_context
from schemas import AgentRequest, AgentResponse, ErrorInfo
from settings import Settings
from tracing import INPUT, OUTPUT, record_content, thread_span


class AgentRunner:
    def __init__(
        self,
        graph: AgentGraph,
        settings: Settings,
        guard: ThreadGuard,
        spend: DailySpend | None = None,
    ) -> None:
        self.threads = AgentThreads(graph)
        self.settings = settings
        self.guard = guard
        self.spend = spend
        self.redactor = Redactor(settings)
        self.priced = price_of(settings.llm_provider, settings.llm_model) is not None

    async def run(self, request: AgentRequest) -> AgentResponse:
        """Run a request to its end. Raises DailyLimitError, ThreadBusyError and
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
            result = await self._run(request, thread_id)
            response = self.redactor.response(result.response)
            self._log_end(response, result.usage, started)
            _trace_end(span, response)
            return response

    async def _run(self, request: AgentRequest, thread_id: UUID) -> RunResult:
        if self.spend is not None:  # SPEC-0012 B-0010, before the lock
            await self.spend.refuse_if_reached()
        async with self.guard.hold(str(thread_id)):
            run = await self._prepare(request, thread_id)
            try:
                async with asyncio.timeout(self.settings.run_timeout_seconds):
                    return await run.invoke()
            except TimeoutError:
                return await self._timed_out(thread_id)
            finally:
                await self.guard.touch(str(thread_id))

    def _log_start(self, request: AgentRequest) -> None:
        audit(
            "run.start",
            kind=_kind(request),
            max_tool_calls=request.max_tool_calls,
            provider=self.settings.llm_provider,
            model=self.settings.llm_model,
        )

    async def _prepare(self, request: AgentRequest, thread_id: UUID) -> PreparedRun:
        """Check the confirmation rules, then log the start: a refused request
        logs nothing (SPEC-0008 B-0002)."""
        if request.confirm is None:
            run = await self.threads.start(
                thread_id, request.message or "", request.max_tool_calls
            )
            self._log_start(request)
            if run.stripped:  # SPEC-0012 B-0015
                audit(
                    "guard",
                    check="invisible_chars",
                    action="stripped",
                    count=run.stripped,
                )
            return run
        run = await self.threads.resume(
            thread_id, request.confirm, request.max_tool_calls
        )
        assert run.pending_action is not None
        self._log_start(request)
        audit(
            "confirm.decision",
            tool=run.pending_action.tool,
            approved=request.confirm,
        )
        return run

    async def _timed_out(self, thread_id: UUID) -> RunResult:
        """B-0006: report the calls made before the timeout and an error."""
        limit = self.settings.run_timeout_seconds
        response = AgentResponse(
            thread_id=thread_id,
            status="error",
            tool_calls=await self.threads.tool_calls(thread_id),
            error=ErrorInfo(
                code="internal",
                message=f"the run took longer than {limit:g} s",
            ),
        )
        return RunResult(response, await self.threads.usage(thread_id))

    def _log_end(
        self, response: AgentResponse, usage: RunUsage, started: float
    ) -> None:
        """run.end, and confirm.wait when the run paused (SPEC-0008 B-0002,
        SPEC-0012 B-0013). cost_usd is left out when the model has no price."""
        if response.pending_action is not None:
            audit("confirm.wait", tool=response.pending_action.tool)
        cost = usage.cost_micro_usd / MICRO_USD if self.priced else None
        audit(
            "run.end",
            status=response.status,
            tool_calls=len(response.tool_calls),
            tokens=usage.tokens,
            estimated=usage.estimated,
            cost_usd=cost,
            ms=elapsed_ms(started),
            error=response.error.code if response.error is not None else None,
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
