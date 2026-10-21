"""Score case runs against their expectations (SPEC-0011 B-0003)."""

import re
import statistics
from collections.abc import Sequence
from typing import Any

from evals.records import Attempt, CaseFailure, CaseResult, RunTotals, TurnResult
from schemas import AgentResponse, EvalCase, EvalExpect, TextMatch

PASS_SHARE = 2 / 3  # a case passes when at least this share of repeats pass

Check = tuple[str, Any, Any]  # check name, expected, actual


def _calls(response: AgentResponse) -> list[tuple[str, dict[str, Any]]]:
    """Every call the model asked for on the turn, with its arguments: the
    ones that ran (or were refused) and the one awaiting confirmation."""
    calls: list[tuple[str, dict[str, Any]]] = [
        (r.name, r.input.model_dump(mode="json")) for r in response.tool_calls
    ]
    if response.pending_action is not None:
        pending = response.pending_action
        calls.append((pending.tool, pending.input.model_dump(mode="json")))
    return calls


def _matches(match: TextMatch, value: Any) -> bool:
    if "equals" in match.model_fields_set:
        return bool(value == match.equals)
    text = value if isinstance(value, str) else str(value)
    if match.contains is not None:
        return match.contains.lower() in text.lower()
    return match.regex is not None and re.search(match.regex, text) is not None


def check_turn(expect: EvalExpect, result: TurnResult) -> list[Check]:
    """The checks of `expect` that `result` fails."""
    response = result.response
    if response is None:
        return [("run", "a response", result.error)]
    failed: list[Check] = []
    ran = [record.name for record in response.tool_calls]
    asked = [name for name, _ in _calls(response)]
    answer = response.answer or ""

    if expect.status is not None and response.status != expect.status:
        failed.append(("status", expect.status, response.status))
    if expect.tools_called is not None:
        same = (
            sorted(ran) == sorted(expect.tools_called)
            if expect.tools_called_any_order
            else ran == expect.tools_called
        )
        if not same:
            failed.append(("tools_called", expect.tools_called, ran))
    for name in expect.tools_not_called or []:
        if name in asked:
            failed.append((f"tools_not_called.{name}", "not called", asked))
    for name, fields in (expect.args or {}).items():
        inputs = [args for tool, args in _calls(response) if tool == name]
        if not any(
            all(_matches(m, args.get(field)) for field, m in fields.items())
            for args in inputs
        ):
            expected = {f: m.model_dump(exclude_unset=True) for f, m in fields.items()}
            failed.append((f"args.{name}", expected, inputs))
    for name, wanted in (expect.tool_results or {}).items():
        results = [
            "ok" if record.error is None else record.error.code
            for record in response.tool_calls
            if record.name == name
        ]
        if not results or any(r != wanted for r in results):
            failed.append((f"tool_results.{name}", wanted, results))
    for text in expect.answer_contains or []:
        if text.lower() not in answer.lower():
            failed.append(("answer_contains", text, answer))
    for text in expect.answer_excludes or []:
        if text.lower() in answer.lower():
            failed.append(("answer_excludes", text, answer))
    if expect.answer_regex is not None and not re.search(expect.answer_regex, answer):
        failed.append(("answer_regex", expect.answer_regex, answer))
    if expect.max_tool_calls is not None and len(ran) > expect.max_tool_calls:
        failed.append(("max_tool_calls", expect.max_tool_calls, len(ran)))
    if expect.max_latency_ms is not None and result.latency_ms > expect.max_latency_ms:
        failed.append(("max_latency_ms", expect.max_latency_ms, result.latency_ms))
    return failed


def check_attempt(case: EvalCase, attempt: Attempt, repeat: int) -> list[CaseFailure]:
    """Failures of one repeat. A block for a turn that never ran fails."""
    failures: list[CaseFailure] = []
    for expect in case.expect_blocks:
        turn = len(case.turns) - 1 if expect.turn is None else expect.turn
        if turn >= len(attempt.turns):
            ended = attempt.turns[-1].error if attempt.turns else None
            failures.append(
                CaseFailure(
                    repeat=repeat, turn=turn, check="turn", expected="run", actual=ended
                )
            )
            continue
        failures += [
            CaseFailure(
                repeat=repeat, turn=turn, check=name, expected=expected, actual=actual
            )
            for name, expected, actual in check_turn(expect, attempt.turns[turn])
        ]
    return failures


def case_score(passes: int, repeats: int, must_pass: bool) -> tuple[float, bool]:
    """The case's score and whether it passed: most repeats for a normal case,
    every repeat for a must_pass one."""
    score = passes / repeats
    passed = passes == repeats if must_pass else score >= PASS_SHARE - 1e-9
    return round(score, 2), passed


def score_case(case: EvalCase, attempts: Sequence[Attempt]) -> CaseResult:
    failures: list[CaseFailure] = []
    passes = 0
    for repeat, attempt in enumerate(attempts):
        found = check_attempt(case, attempt, repeat)
        failures += found
        passes += not found
    score, passed = case_score(passes, len(attempts), case.must_pass)
    turns = [turn for attempt in attempts for turn in attempt.turns]
    calls = sum(len(t.response.tool_calls) for t in turns if t.response is not None)
    input_tokens = sum(t.input_tokens for t in turns)
    output_tokens = sum(t.output_tokens for t in turns)
    return CaseResult(
        id=case.id,
        score=score,
        passed=passed,
        must_pass=case.must_pass,
        failures=failures,
        tool_calls=round(calls / len(attempts), 2),
        latency_ms=round(sum(t.latency_ms for t in turns) / len(attempts)),
        input_tokens=input_tokens or None,
        output_tokens=output_tokens or None,
    )


def totals(
    cases: Sequence[EvalCase],
    results: Sequence[CaseResult],
    latencies: Sequence[int],
) -> RunTotals:
    """Run score = mean case score, also per tag; latency percentiles are per
    request."""
    by_tag: dict[str, list[float]] = {}
    for case, result in zip(cases, results, strict=True):
        for tag in case.tags:
            by_tag.setdefault(tag, []).append(result.score)
    tokens = sum((r.input_tokens or 0) + (r.output_tokens or 0) for r in results)
    return RunTotals(
        score=round(statistics.fmean(r.score for r in results), 3) if results else 0,
        by_tag={
            tag: round(statistics.fmean(s), 3) for tag, s in sorted(by_tag.items())
        },
        must_pass_failures=[r.id for r in results if r.must_pass and not r.passed],
        latency_p50_ms=_percentile(latencies, 50),
        latency_p95_ms=_percentile(latencies, 95),
        tokens=tokens or None,
    )


def _percentile(values: Sequence[int], percent: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(percent / 100 * (len(ordered) - 1)))
    return ordered[index]
