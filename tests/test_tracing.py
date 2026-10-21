"""Tracing: the span tree, failures, content switch and export isolation
(SPEC-0010). Spans go to an in-memory exporter; nothing needs Langfuse."""

import json
import threading
import time
from collections.abc import Sequence
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from openinference.instrumentation.langchain import LangChainInstrumentor
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from pydantic import SecretStr

from agent.graph import build_graph
from agent_support import (
    Capture,
    MakeClient,
    StartTracing,
    fetch,
    finished_spans,
    note,
    tool_turn,
)
from api.deps import get_graph
from app import app
from fakes import RaisingChatModel
from settings import Settings, get_settings
from tools import TOOLS
from tools.base import ToolContext
from tracing import otlp_target

RUN = "/v1/agent/run"
USAGE = {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15}
LLM_SPAN = "ScriptedChatModel"  # OpenInference names it after the model class


def named(spans: Sequence[ReadableSpan], name: str) -> list[ReadableSpan]:
    return sorted((s for s in spans if s.name == name), key=lambda s: s.start_time or 0)


def one(spans: Sequence[ReadableSpan], name: str) -> ReadableSpan:
    found = named(spans, name)
    assert len(found) == 1, (name, [s.name for s in spans])
    return found[0]


def parent(spans: Sequence[ReadableSpan], span: ReadableSpan) -> str | None:
    """The name of the span's parent, or None for a root span."""
    if span.parent is None:
        return None
    ids = {s.context.span_id: s.name for s in spans if s.context is not None}
    return ids[span.parent.span_id]


def dump(spans: Sequence[ReadableSpan]) -> str:
    """Everything recorded on the spans, as text to search."""
    return json.dumps(
        [
            [
                s.name,
                dict(s.attributes or {}),
                [dict(e.attributes or {}) for e in s.events],
                s.status.description,
            ]
            for s in spans
        ],
        default=str,
    )


@pytest.mark.spec("SPEC-0010", "B-0001", "AT-0001")
async def test_tracing_off_records_nothing(
    make_client: MakeClient,
    start_tracing: StartTracing,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exporter = InMemorySpanExporter()
    tracing = start_tracing(exporter, enabled=False)
    client, _ = make_client([tool_turn(fetch()), AIMessage(content="done")])

    response = await client.post(RUN, json={"message": "hi"})

    assert response.status_code == 200
    assert finished_spans(tracing, exporter) == []
    assert not LangChainInstrumentor().is_instrumented_by_opentelemetry
    assert capsys.readouterr().out == ""  # no OpenInference warnings either


@pytest.mark.spec("SPEC-0010", "B-0002", "B-0003", "B-0006", "AT-0002")
async def test_run_span_tree(
    make_client: MakeClient, start_tracing: StartTracing
) -> None:
    exporter = InMemorySpanExporter()
    tracing = start_tracing(exporter)
    first = tool_turn(fetch()).model_copy(update={"usage_metadata": USAGE})
    client, _ = make_client([first, AIMessage(content="done")])
    thread = str(uuid4())

    response = await client.post(
        RUN, json={"thread_id": thread, "message": "hi"}, headers={"X-Request-ID": "r1"}
    )

    assert response.status_code == 200
    spans = finished_spans(tracing, exporter)
    server = one(spans, "POST /v1/agent/run")
    run = one(spans, "agent.run")
    tool = one(spans, "agent.tool fetch_site")
    assert parent(spans, server) is None
    assert server.attributes is not None and server.attributes["request_id"] == "r1"
    assert parent(spans, run) == server.name
    assert parent(spans, one(spans, "LangGraph")) == "agent.run"
    assert [parent(spans, s) for s in named(spans, "model")] == ["LangGraph"] * 2
    assert parent(spans, one(spans, "tools")) == "LangGraph"
    assert parent(spans, tool) == "tools"
    llm = named(spans, LLM_SPAN)
    assert [parent(spans, s) for s in llm] == ["model", "model"]

    assert run.attributes is not None
    assert run.attributes["session.id"] == thread
    assert run.attributes["thread"] == thread
    assert run.attributes["kind"] == "message"
    assert run.attributes["status"] == "ok"
    assert run.attributes["tool_calls"] == 1
    assert tool.attributes is not None
    assert tool.attributes["result"] == "ok"
    assert tool.attributes["call_id"] == "call-fetch_site-0"

    # B-0003: token counts as reported; none when the reply has no usage.
    assert llm[0].attributes is not None and llm[1].attributes is not None
    assert llm[0].attributes["llm.token_count.prompt"] == 11
    assert llm[0].attributes["llm.token_count.completion"] == 4
    assert not any(k.startswith("llm.token_count") for k in llm[1].attributes)


@pytest.mark.spec("SPEC-0010", "B-0002")
async def test_confirmation_spans(
    make_client: MakeClient, start_tracing: StartTracing
) -> None:
    """A pause is a normal end: agent.confirm is awaiting and not an error."""
    exporter = InMemorySpanExporter()
    tracing = start_tracing(exporter)
    client, _ = make_client([tool_turn(note("x")), AIMessage(content="saved")])
    thread = str(uuid4())

    paused = await client.post(RUN, json={"thread_id": thread, "message": "note x"})
    spans = finished_spans(tracing, exporter)
    exporter.clear()
    resumed = await client.post(RUN, json={"thread_id": thread, "confirm": True})
    resumed_spans = finished_spans(tracing, exporter)

    assert paused.json()["status"] == "awaiting_confirmation"
    awaiting = one(spans, "agent.confirm")
    assert parent(spans, awaiting) == "confirm"
    assert awaiting.attributes is not None
    assert awaiting.attributes["decision"] == "awaiting"
    assert awaiting.attributes["tool"] == "add_note"
    assert awaiting.status.status_code is StatusCode.UNSET
    run = one(spans, "agent.run")
    assert run.attributes is not None
    assert run.attributes["status"] == "awaiting_confirmation"
    assert run.status.status_code is StatusCode.UNSET

    assert resumed.json()["status"] == "ok"
    approved = one(resumed_spans, "agent.confirm")
    assert approved.attributes is not None
    assert approved.attributes["decision"] == "approved"
    assert parent(resumed_spans, one(resumed_spans, "agent.tool add_note")) == "confirm"


@pytest.mark.spec("SPEC-0010", "B-0002", "AT-0003")
async def test_failures_recorded(
    make_client: MakeClient, start_tracing: StartTracing, tool_context: ToolContext
) -> None:
    exporter = InMemorySpanExporter()
    tracing = start_tracing(exporter)

    # A run that times out.
    client, agent = make_client([AIMessage(content="too late")], delay=1)
    slow = agent.ctx.settings.model_copy(update={"run_timeout_seconds": 0.1})
    app.dependency_overrides[get_settings] = lambda: slow
    timed_out = await client.post(RUN, json={"message": "hi"})
    spans = finished_spans(tracing, exporter)
    exporter.clear()

    assert timed_out.json()["status"] == "error"
    run = one(spans, "agent.run")
    assert run.status.status_code is StatusCode.ERROR
    assert run.attributes is not None and run.attributes["error"] == "internal"

    # The provider is down: 503.
    down = RaisingChatModel(error=httpx.ConnectError("refused"))
    graph = build_graph(down, tool_context, TOOLS, InMemorySaver())
    app.dependency_overrides[get_graph] = lambda: graph
    unavailable = await client.post(RUN, json={"message": "hi"})
    spans = finished_spans(tracing, exporter)

    assert unavailable.status_code == 503
    server = one(spans, "POST /v1/agent/run")
    assert server.status.status_code is StatusCode.ERROR
    assert server.attributes is not None
    assert server.attributes["error.code"] == "dependency_unavailable"
    assert one(spans, "agent.run").status.status_code is StatusCode.ERROR
    assert one(spans, "RaisingChatModel").status.status_code is StatusCode.ERROR


@pytest.mark.spec("SPEC-0010", "B-0004", "AT-0004")
@pytest.mark.parametrize("content", [True, False])
async def test_trace_content_switch(
    make_client: MakeClient, start_tracing: StartTracing, content: bool
) -> None:
    exporter = InMemorySpanExporter()
    tracing = start_tracing(exporter, content=content)
    replies = [tool_turn(fetch("TOOL-MARKER")), AIMessage(content="REPLY-MARKER")]
    client, agent = make_client(replies)
    keyed = agent.ctx.settings.model_copy(
        update={"anthropic_api_key": "KEY-MARKER", "tavily_api_key": "KEY-MARKER"}
    )
    app.dependency_overrides[get_settings] = lambda: keyed

    response = await client.post(RUN, json={"message": "MSG-MARKER"})

    assert response.json()["answer"] == "REPLY-MARKER"
    recorded = dump(finished_spans(tracing, exporter))
    for marker in ("MSG-MARKER", "REPLY-MARKER", "TOOL-MARKER"):
        assert (marker in recorded) is content, marker
    assert "KEY-MARKER" not in recorded


class FailingExporter(InMemorySpanExporter):
    """Fails every export, raising on the first and returning FAILURE after."""

    def __init__(self) -> None:
        super().__init__()
        self.exports = 0

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.exports += 1
        if self.exports == 1:
            raise ConnectionError("backend down")
        return SpanExportResult.FAILURE


class HangingExporter(InMemorySpanExporter):
    """Blocks every export until released."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.entered.set()
        self.release.wait(10)
        return SpanExportResult.SUCCESS


@pytest.mark.spec("SPEC-0010", "B-0005", "AT-0005")
async def test_exporter_failure_isolated(
    make_client: MakeClient,
    start_tracing: StartTracing,
    capture: Capture,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture("info")
    client, _ = make_client([AIMessage(content=str(n)) for n in range(4)])

    failing = FailingExporter()
    tracing = start_tracing(failing)
    for _ in range(2):
        assert (await client.post(RUN, json={"message": "hi"})).status_code == 200
        tracing.force_flush()
    assert failing.exports == 2
    warnings = [r for r in caplog.records if r.getMessage().startswith("trace.")]
    assert len(warnings) == 1  # at most once a minute
    assert warnings[0].levelname == "WARNING"
    assert "trace.export_failed" in warnings[0].getMessage()
    assert "backend down" in warnings[0].getMessage()
    tracing.shutdown()

    # The batch processor exports every 10 ms; its export then hangs.
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "10")
    hanging = HangingExporter()
    start_tracing(hanging)
    try:
        assert (await client.post(RUN, json={"message": "hi"})).status_code == 200
        assert hanging.entered.wait(5)
        started = time.perf_counter()
        response = await client.post(RUN, json={"message": "hi"})
        assert response.status_code == 200
        assert time.perf_counter() - started < 1
    finally:
        hanging.release.set()


@pytest.mark.spec("SPEC-0010", "B-0002")
async def test_thread_spans(
    make_client: MakeClient, start_tracing: StartTracing
) -> None:
    """The thread endpoints have their own spans; the summary call is under compact."""
    exporter = InMemorySpanExporter()
    tracing = start_tracing(exporter)
    replies = [AIMessage(content=text) for text in ("a", "b", "summary")]
    client, _ = make_client(replies)
    thread = str(uuid4())
    for message in ("one", "two"):
        await client.post(RUN, json={"thread_id": thread, "message": message})

    read = await client.get(f"/v1/threads/{thread}")
    compacted = await client.post(
        f"/v1/threads/{thread}/compact", json={"keep_turns": 1}
    )
    deleted = await client.delete(f"/v1/threads/{thread}")

    assert (read.status_code, compacted.status_code, deleted.status_code) == (
        200,
        200,
        204,
    )
    spans = finished_spans(tracing, exporter)
    compact = one(spans, "thread.compact")
    for span in (one(spans, "thread.read"), compact, one(spans, "thread.delete")):
        assert span.attributes is not None
        assert span.attributes["session.id"] == thread
    assert compact.attributes is not None
    assert compact.attributes["messages_removed"] == 2
    summary_call = named(spans, LLM_SPAN)[-1]
    assert parent(spans, summary_call) == "thread.compact"


@pytest.mark.spec("SPEC-0010", "B-0002")
def test_otlp_target() -> None:
    """Langfuse's OTLP path with basic auth, unless an OTLP endpoint is set."""
    langfuse = Settings(
        _env_file=None,  # type: ignore[call-arg]
        langfuse_host="http://lf:3000/",
        langfuse_public_key=SecretStr("pk"),
        langfuse_secret_key=SecretStr("sk"),
    )
    other = langfuse.model_copy(
        update={
            "otel_exporter_otlp_endpoint": "http://collector:4318",
            "otel_exporter_otlp_headers": SecretStr("x-key=abc%20d"),
        }
    )

    assert otlp_target(langfuse) == (
        "http://lf:3000/api/public/otel/v1/traces",
        {"Authorization": "Basic cGs6c2s="},  # base64 of "pk:sk"
    )
    assert otlp_target(other) == (
        "http://collector:4318/v1/traces",
        {"x-key": "abc d"},
    )
