"""The eval harness: dataset, stubbed tools, case runner and scoring (SPEC-0011).

No test calls a real model: cases run on the scripted fake (SPEC-0005).
"""

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from redis.asyncio import Redis

from agent.persistence import RedisThreadGuard, open_checkpointer
from agent_support import LocalThreadGuard, fetch, note, tool_turn
from evals.dataset import DatasetError, load_cases, select
from evals.records import Attempt, RunRecord, TurnResult, write_run
from evals.runner import CaseRunner, run_cases
from evals.scoring import case_score, check_turn, score_case
from fakes import ScriptedChatModel
from schemas import (
    AddNoteCall,
    AddNoteInput,
    AddNoteOutput,
    AgentResponse,
    EvalCase,
    EvalExpect,
    PendingAction,
    ToolError,
    WebSearchCall,
    WebSearchInput,
    WebSearchOutput,
    WebSearchResult,
)
from settings import Settings
from tools.base import ToolContext

THREAD = "6f1c2a9e-4b7d-4e2a-9c1f-3d5e8a7b2c15"
SEARCH = ("web_search", {"query": "harness agents"})


def case(**fields: Any) -> EvalCase:
    base: dict[str, Any] = {
        "id": "EV-0001",
        "description": "a case",
        "turns": [{"message": "hi"}],
    }
    return EvalCase.model_validate(base | fields)


def write_cases(directory: Path, name: str, *lines: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("\n".join(lines) + "\n")


def line(**fields: Any) -> str:
    return case(**fields).model_dump_json(exclude_defaults=True)


def runner(
    tool_context: ToolContext,
    tmp_path: Path,
    replies: list[AIMessage],
    transport: httpx.AsyncBaseTransport | None = None,
) -> CaseRunner:
    return CaseRunner(
        ScriptedChatModel(replies=replies),
        tool_context.settings,
        InMemorySaver(),
        LocalThreadGuard(),
        tmp_path / "eval-notes",
        transport,
    )


@pytest.mark.spec("SPEC-0011", "B-0001", "AT-0001")
def test_dataset_validation(tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    write_cases(
        cases,
        "a.jsonl",
        line(id="EV-0001"),
        json.dumps({"id": "EV-0002", "description": "no turns", "turns": []}),
        "",
        json.dumps({"id": "bad", "description": "x", "turns": [{"message": "hi"}]}),
    )
    write_cases(
        cases,
        "b.jsonl",
        line(id="EV-0001"),
        json.dumps(
            {
                "id": "EV-0003",
                "description": "x",
                "turns": [{"message": "hi"}],
                "expect": {"turn": 1, "status": "ok"},
            }
        ),
    )

    with pytest.raises(DatasetError) as raised:
        load_cases(cases)

    problems = raised.value.problems
    assert len(problems) == 4
    assert problems[0].startswith("a.jsonl:2: turns:")
    assert problems[1].startswith("a.jsonl:4: id:")
    assert problems[2] == "b.jsonl:1: duplicate id EV-0001 (also a.jsonl:1)"
    assert problems[3].startswith("b.jsonl:2:") and "past the last turn" in problems[3]


@pytest.mark.spec("SPEC-0011", "B-0001")
def test_dataset_loads_and_selects(tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    write_cases(
        cases,
        "a.jsonl",
        line(id="EV-0001", tags=["search"]),
        line(id="EV-0002", tags=["confirmation"]),
        line(id="EV-0003"),
    )

    loaded = load_cases(cases)

    assert [c.id for c in loaded] == ["EV-0001", "EV-0002", "EV-0003"]
    assert [c.id for c in select(loaded, ["confirmation"], ["EV-0003"])] == [
        "EV-0002",
        "EV-0003",
    ]
    assert select(loaded) == loaded


@pytest.mark.spec("SPEC-0011", "B-0002", "AT-0002")
async def test_stub_tools(tool_context: ToolContext, tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    canned = WebSearchOutput(
        results=[WebSearchResult(title="T", url="https://x.example/", snippet="S")]
    )
    stub_case = case(
        turns=[{"message": "search, fetch and note"}, {"confirm": True}],
        tool_outputs={
            "web_search": canned.model_dump(mode="json"),
            "fetch_site": {"code": "upstream_error", "message": "site down"},
        },
    )
    replies = [
        tool_turn(SEARCH, fetch("page"), note("kept")),
        AIMessage(content="done"),
    ]
    case_runner = runner(tool_context, tmp_path, replies, httpx.MockTransport(serve))

    attempt = await case_runner.attempt(stub_case)

    paused, done = (turn.response for turn in attempt.turns)
    assert paused is not None and paused.status == "awaiting_confirmation"
    assert done is not None and done.status == "ok"
    search, fetched = paused.tool_calls
    assert search.output == canned
    assert fetched.error == ToolError(code="upstream_error", message="site down")
    assert requests == []  # stubs make no network call
    # add_note isn't stubbed here: it ran, and wrote to the case's temporary file.
    [notes_file] = (tmp_path / "eval-notes").iterdir()
    assert notes_file.read_text().endswith("\tkept\n")
    assert tool_context.settings.notes_path.exists() is False
    model = case_runner.model
    assert isinstance(model, ScriptedChatModel)
    results = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
    assert json.loads(str(results[0].content)) == canned.model_dump(mode="json")


@pytest.mark.spec("SPEC-0011", "B-0002")
async def test_case_runner_records_errors_and_tokens(
    tool_context: ToolContext, tmp_path: Path
) -> None:
    usage = {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
    reply = AIMessage(
        content="hello", usage_metadata=usage, response_metadata={"model_name": "m"}
    )
    two_turns = case(turns=[{"message": "hi"}, {"confirm": True}])

    attempt = await runner(tool_context, tmp_path, [reply]).attempt(two_turns)

    first, second = attempt.turns
    assert first.response is not None and first.response.answer == "hello"
    assert (first.input_tokens, first.output_tokens) == (10, 4)
    # Confirming with nothing pending raises; the attempt records it and stops.
    assert second.response is None
    assert second.error is not None
    assert second.error.startswith("ConfirmationStateError")


def response(**fields: Any) -> AgentResponse:
    return AgentResponse.model_validate({"thread_id": THREAD, "status": "ok"} | fields)


SEARCH_CALL = WebSearchCall(
    id="c1",
    input=WebSearchInput(query="Cline and Aider"),
    output=WebSearchOutput(results=[]),
)
REJECTED_NOTE = AddNoteCall(
    id="c2",
    input=AddNoteInput(text="buy milk"),
    error=ToolError(code="rejected", message="declined"),
)
WRITTEN_NOTE = AddNoteCall(
    id="c3",
    input=AddNoteInput(text="buy bread"),
    output=AddNoteOutput(written=True, line="t\tbuy bread"),
)
ANSWERED = response(
    answer="Cline and Aider are coding agents.",
    tool_calls=[SEARCH_CALL, REJECTED_NOTE],
)
PAUSED = response(
    status="awaiting_confirmation",
    pending_action=PendingAction(input=AddNoteInput(text="buy milk")),
)


@pytest.mark.spec("SPEC-0011", "B-0003", "AT-0003")
@pytest.mark.parametrize(
    ("expect", "result", "failed"),
    [
        ({"status": "ok"}, ANSWERED, []),
        ({"status": "truncated"}, ANSWERED, ["status"]),
        ({"tools_called": ["web_search", "add_note"]}, ANSWERED, []),
        ({"tools_called": ["add_note", "web_search"]}, ANSWERED, ["tools_called"]),
        (
            {
                "tools_called": ["add_note", "web_search"],
                "tools_called_any_order": True,
            },
            ANSWERED,
            [],
        ),
        ({"tools_called": []}, PAUSED, []),
        ({"tools_not_called": ["fetch_site"]}, ANSWERED, []),
        ({"tools_not_called": ["add_note"]}, PAUSED, ["tools_not_called.add_note"]),
        ({"args": {"web_search": {"query": {"contains": "aider"}}}}, ANSWERED, []),
        (
            {"args": {"web_search": {"query": {"contains": "goose"}}}},
            ANSWERED,
            ["args.web_search"],
        ),
        ({"args": {"add_note": {"text": {"equals": "buy milk"}}}}, PAUSED, []),
        ({"args": {"add_note": {"text": {"regex": "^buy"}}}}, PAUSED, []),
        (
            {"args": {"fetch_site": {"url": {"contains": "x"}}}},
            ANSWERED,
            ["args.fetch_site"],
        ),
        ({"tool_results": {"add_note": "rejected", "web_search": "ok"}}, ANSWERED, []),
        ({"tool_results": {"add_note": "ok"}}, ANSWERED, ["tool_results.add_note"]),
        ({"tool_results": {"add_note": "ok"}}, PAUSED, ["tool_results.add_note"]),
        ({"answer_contains": ["CLINE", "aider"]}, ANSWERED, []),
        ({"answer_contains": ["goose"]}, ANSWERED, ["answer_contains"]),
        ({"answer_excludes": ["goose"]}, ANSWERED, []),
        ({"answer_excludes": ["aider"]}, ANSWERED, ["answer_excludes"]),
        ({"answer_regex": r"coding agents\.$"}, ANSWERED, []),
        ({"answer_regex": r"^Aider"}, ANSWERED, ["answer_regex"]),
        ({"max_tool_calls": 2}, ANSWERED, []),
        ({"max_tool_calls": 1}, ANSWERED, ["max_tool_calls"]),
        ({"max_latency_ms": 500}, ANSWERED, []),
        ({"max_latency_ms": 100}, ANSWERED, ["max_latency_ms"]),
    ],
)
def test_scorers(
    expect: dict[str, Any], result: AgentResponse, failed: list[str]
) -> None:
    checks = check_turn(
        EvalExpect.model_validate(expect), TurnResult(response=result, latency_ms=300)
    )

    assert [name for name, _, _ in checks] == failed
    for _, expected, actual in checks:
        assert expected is not None and actual is not None  # both recorded


@pytest.mark.spec("SPEC-0011", "B-0003", "AT-0003")
def test_scorers_turns() -> None:
    multi = case(
        turns=[{"message": "note"}, {"confirm": True}, {"message": "again"}],
        expect=[
            {"turn": 0, "status": "awaiting_confirmation"},
            {"turn": 1, "tool_results": {"add_note": "ok"}},
            {"status": "ok"},
        ],
    )
    done = response(answer="noted", tool_calls=[WRITTEN_NOTE])
    raised = TurnResult(response=None, latency_ms=6, error="InvalidUpdateError: x")

    full = Attempt([TurnResult(PAUSED, 9), TurnResult(done, 9), TurnResult(done, 9)])
    cut = Attempt([TurnResult(PAUSED, 9), raised])
    result = score_case(multi, [full, cut])

    assert [(f.repeat, f.turn, f.check) for f in result.failures] == [
        (1, 1, "run"),
        (1, 2, "turn"),
    ]
    assert result.failures[0].actual == "InvalidUpdateError: x"
    assert result.score == 0.5 and not result.passed
    assert result.tool_calls == 1  # two calls over two repeats
    assert result.latency_ms == 21  # (27 + 15) / 2


@pytest.mark.spec("SPEC-0011", "B-0003", "AT-0004")
def test_repeat_scoring() -> None:
    passing = Attempt([TurnResult(ANSWERED, 10)])
    failing = Attempt([TurnResult(PAUSED, 10)])
    attempts = [passing, failing, passing]
    normal = case(expect={"status": "ok"})
    must = case(expect={"status": "ok"}, must_pass=True)

    assert case_score(2, 3, must_pass=False) == (0.67, True)
    assert case_score(2, 3, must_pass=True) == (0.67, False)
    assert case_score(1, 3, must_pass=False) == (0.33, False)
    assert (
        score_case(normal, attempts).score,
        score_case(normal, attempts).passed,
    ) == (
        0.67,
        True,
    )
    assert score_case(must, attempts).passed is False
    assert score_case(must, [passing] * 3).passed is True


@pytest.mark.spec("SPEC-0011", "B-0003", "B-0005")
async def test_run_record(tool_context: ToolContext, tmp_path: Path) -> None:
    cases = [
        case(id="EV-0001", tags=["chat"], expect={"answer_contains": ["hi"]}),
        case(
            id="EV-0002",
            tags=["chat", "safety"],
            must_pass=True,
            expect={"status": "error"},
        ),
    ]
    replies = [AIMessage(content=text) for text in ("hi", "hi", "hello", "hello")]
    case_runner = runner(tool_context, tmp_path, replies)
    lines: list[str] = []

    record = await run_cases(
        case_runner, cases, 2, partial=True, git=("abc123", True), progress=lines.append
    )
    path = write_run(record, tmp_path / "runs")

    assert [c.score for c in record.cases] == [1.0, 0.0]
    assert record.totals.score == 0.5
    assert record.totals.by_tag == {"chat": 0.5, "safety": 0.0}
    assert record.totals.must_pass_failures == ["EV-0002"]
    assert (record.git_sha, record.dirty, record.partial, record.repeats) == (
        "abc123",
        True,
        True,
        2,
    )
    assert lines[1].startswith("EV-0002 FAIL 0.00")
    assert path.name == f"{record.run_id}.json"
    assert record.run_id.endswith("__ollama__llama3.1")
    assert RunRecord.model_validate_json(path.read_text()) == record


@pytest.fixture
async def redis(redis_url: str) -> AsyncGenerator[Redis]:
    client = Redis.from_url(redis_url)
    yield client
    await client.aclose()


@pytest.mark.redis
@pytest.mark.spec("SPEC-0011", "B-0002")
async def test_cases_run_on_redis(
    tool_context: ToolContext, tmp_path: Path, redis_url: str, redis: Redis
) -> None:
    """Two confirms and a reject on one thread, on the Redis checkpointer; each
    repeat's thread is deleted after it."""
    settings: Settings = tool_context.settings.model_copy(
        update={"redis_url": redis_url}
    )
    confirms = case(
        must_pass=True,
        turns=[
            {"message": "one"},
            {"confirm": True},
            {"message": "two"},
            {"confirm": True},
            {"message": "three"},
            {"confirm": False},
        ],
        expect=[
            {"turn": 1, "tool_results": {"add_note": "ok"}},
            {"turn": 3, "tool_results": {"add_note": "ok"}},
            {"turn": 5, "status": "ok", "tool_results": {"add_note": "rejected"}},
        ],
    )
    replies: list[AIMessage] = []
    for text in ("one", "two", "three"):
        replies += [tool_turn(note(text)), AIMessage(content=f"done {text}")]

    async with open_checkpointer(settings) as saver:
        case_runner = CaseRunner(
            ScriptedChatModel(replies=replies),
            settings,
            saver,
            RedisThreadGuard(redis, settings),
            tmp_path / "eval-notes",
        )
        attempt = await case_runner.attempt(confirms)

    assert score_case(confirms, [attempt]).failures == []
    responses = [t.response for t in attempt.turns]
    thread_id = str(responses[0].thread_id if responses[0] else "")
    assert [k async for k in redis.scan_iter(match=f"*{thread_id}*")] == []
