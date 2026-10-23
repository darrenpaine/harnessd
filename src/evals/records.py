"""What a case run produced, and the run file written for each eval run
(SPEC-0011, "Run file")."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from schemas import AgentResponse


@dataclass
class TurnResult:
    """One request of a case: the response, or the error the runner raised."""

    response: AgentResponse | None
    latency_ms: int
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Attempt:
    """One repeat of a case, on its own thread. Turns after a raised error are
    not run, so `turns` can be shorter than the case's."""

    turns: list[TurnResult] = field(default_factory=list)


class CaseFailure(BaseModel):
    """A check that failed on one repeat (B-0003)."""

    repeat: int
    turn: int
    check: str
    expected: Any = None
    actual: Any = None


class CaseResult(BaseModel):
    id: str
    score: float
    passed: bool
    must_pass: bool
    failures: list[CaseFailure]
    judge_score: float | None = None
    tool_calls: float  # mean per repeat
    latency_ms: int  # mean per repeat, all turns
    input_tokens: int | None = None  # all repeats
    output_tokens: int | None = None


class RunTotals(BaseModel):
    score: float
    by_tag: dict[str, float]
    must_pass_failures: list[str]
    latency_p50_ms: int  # per request
    latency_p95_ms: int
    tokens: int | None


class RunRecord(BaseModel):
    run_id: str
    started_at: datetime
    git_sha: str
    dirty: bool
    provider: str
    model: str
    judge_model: str | None = None
    repeats: int
    partial: bool
    cases: list[CaseResult]
    totals: RunTotals


def model_key(provider: str, model: str) -> str:
    """`<provider>__<model>`, safe as a file name; names a baseline file."""
    return f"{provider}__{re.sub(r'[^A-Za-z0-9._-]', '-', model)}"


def run_id(started_at: datetime, provider: str, model: str) -> str:
    """`<date>T<time>__<provider>__<model>`, safe as a file name."""
    return f"{started_at:%Y-%m-%dT%H%M%S}__{model_key(provider, model)}"


def write_run(record: RunRecord, directory: Path) -> Path:
    """Write one run file (B-0005) and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record.run_id}.json"
    path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
