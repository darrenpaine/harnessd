"""Run eval cases through the shipped agent (SPEC-0011 B-0002).

Each repeat of a case runs on a new thread through `AgentRunner`, with the
case's tools and a notes file of its own, and the thread is deleted after.
"""

import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from langchain_core.callbacks import get_usage_metadata_callback
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from agent.agent_threads import AgentThreads
from agent.graph import build_graph
from agent.persistence import ThreadGuard
from agent.runner import AgentRunner
from evals.records import Attempt, RunRecord, TurnResult, run_id
from evals.scoring import score_case, totals
from evals.stubs import case_tools
from logs import elapsed_ms
from schemas import AgentRequest, EvalCase, EvalTurn
from settings import Settings
from tools.base import ToolContext


class CaseRunner:
    def __init__(
        self,
        model: BaseChatModel,
        settings: Settings,
        checkpointer: BaseCheckpointSaver[Any],
        guard: ThreadGuard,
        notes_dir: Path,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.settings = settings
        self.checkpointer = checkpointer
        self.guard = guard
        self.notes_dir = notes_dir
        self.transport = transport

    async def attempt(self, case: EvalCase) -> Attempt:
        """Run the case's turns in order on a new thread. A turn that raises is
        recorded and ends the attempt."""
        thread_id = uuid4()
        settings = self.settings.model_copy(
            update={"notes_path": self.notes_dir / f"{thread_id}.txt"}
        )
        ctx = ToolContext(settings=settings, transport=self.transport)
        graph = build_graph(self.model, ctx, case_tools(case), self.checkpointer)
        runner = AgentRunner(graph, settings, self.guard)
        attempt = Attempt()
        try:
            for turn in case.turns:
                result = await _turn(runner, _request(thread_id, turn))
                attempt.turns.append(result)
                if result.response is None:
                    break
        finally:
            await AgentThreads(graph).delete(thread_id)
        return attempt


def _request(thread_id: Any, turn: EvalTurn) -> AgentRequest:
    fields = turn.model_dump(exclude_none=True)
    return AgentRequest.model_validate({"thread_id": thread_id, **fields})


async def _turn(runner: AgentRunner, request: AgentRequest) -> TurnResult:
    started = time.perf_counter()
    with get_usage_metadata_callback() as usage:
        try:
            response = await runner.run(request)
            error = None
        except Exception as exc:
            response, error = None, f"{type(exc).__name__}: {exc}"
    used = list(usage.usage_metadata.values())
    return TurnResult(
        response=response,
        error=error,
        latency_ms=elapsed_ms(started),
        input_tokens=sum(u["input_tokens"] for u in used),
        output_tokens=sum(u["output_tokens"] for u in used),
    )


async def run_cases(
    runner: CaseRunner,
    cases: Sequence[EvalCase],
    repeats: int,
    *,
    partial: bool,
    git: tuple[str, bool] = ("unknown", False),
    progress: Callable[[str], None] | None = None,
) -> RunRecord:
    """Run every case `repeats` times and score the run (B-0003)."""
    started_at = datetime.now(UTC)
    results = []
    latencies: list[int] = []
    for case in cases:
        attempts = [await runner.attempt(case) for _ in range(repeats)]
        latencies += [t.latency_ms for a in attempts for t in a.turns]
        result = score_case(case, attempts)
        results.append(result)
        if progress is not None:
            mark = "pass" if result.passed else "FAIL"
            progress(f"{case.id} {mark} {result.score:.2f}  {case.description}")
    settings = runner.settings
    return RunRecord(
        run_id=run_id(started_at, settings.llm_provider, settings.llm_model),
        started_at=started_at,
        git_sha=git[0],
        dirty=git[1],
        provider=settings.llm_provider,
        model=settings.llm_model,
        repeats=repeats,
        partial=partial,
        cases=results,
        totals=totals(cases, results, latencies),
    )
