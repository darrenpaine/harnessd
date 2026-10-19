"""OpenTelemetry tracing: setup, export, and the app's own spans (SPEC-0010).

OTel's global tracer provider can be set only once, and FastAPI's middleware is
built once, but tracing is configured at startup from settings and tests turn it
on and off. So the app and our own spans use `PROVIDER`, which hands each span to
whatever provider `configure_tracing` last installed; with tracing off that is
OTel's no-op provider, which records nothing (B-0001).

LangChain and LangGraph calls are traced by OpenInference, which is patched in by
`configure_tracing` and removed again by `Tracing.shutdown`.
"""

import base64
import json
import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from openinference.instrumentation import TraceConfig
from openinference.instrumentation.langchain import (
    LangChainInstrumentor,
)
from openinference.instrumentation.langchain import (
    get_current_span as langchain_span,
)
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, SpanKind, StatusCode
from opentelemetry.util.re import parse_env_headers
from opentelemetry.util.types import Attributes, AttributeValue
from pydantic import BaseModel

from logs import event, get_logger
from schemas import ToolError
from settings import Settings

SERVICE = "harnessd"
LANGFUSE_PATH = "/api/public/otel/v1/traces"
TRACES_PATH = "/v1/traces"  # appended to OTEL_EXPORTER_OTLP_ENDPOINT, as OTel does
EXPORT_LOG_SECONDS = 60.0
NOT_TRACED = "health"  # paths with no server span: health checks poll constantly

# OpenInference attribute names, which Langfuse maps to sessions, inputs and outputs.
SESSION_ID = "session.id"
SPAN_KIND = "openinference.span.kind"
INPUT = "input.value"
OUTPUT = "output.value"

log = get_logger("tracing")


class _SwitchingTracer(trace.Tracer):
    """Starts each span on the provider current at the time, not at creation."""

    def __init__(self, switch: SwitchingTracerProvider, name: str) -> None:
        self._switch = switch
        self._name = name

    def _tracer(self) -> trace.Tracer:
        return self._switch.current.get_tracer(self._name)

    def start_span(
        self,
        name: str,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Attributes = None,
        links: Sequence[trace.Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> Span:
        return self._tracer().start_span(
            name,
            context,
            kind,
            attributes,
            links,
            start_time,
            record_exception,
            set_status_on_exception,
        )

    @contextmanager
    def start_as_current_span(  # type: ignore[override]
        self,
        name: str,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Attributes = None,
        links: Sequence[trace.Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        end_on_exit: bool = True,
    ) -> Iterator[Span]:
        with self._tracer().start_as_current_span(
            name,
            context,
            kind,
            attributes,
            links,
            start_time,
            record_exception,
            set_status_on_exception,
            end_on_exit,
        ) as span:
            yield span


class SwitchingTracerProvider(trace.TracerProvider):
    """A tracer provider whose tracers follow `current`, which
    `configure_tracing` replaces. `content` is TRACE_CONTENT (B-0004)."""

    def __init__(self) -> None:
        self.current: trace.TracerProvider = trace.NoOpTracerProvider()
        self.content = False

    def get_tracer(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: Attributes = None,
    ) -> trace.Tracer:
        return _SwitchingTracer(self, instrumenting_module_name)


PROVIDER = SwitchingTracerProvider()
TRACER = PROVIDER.get_tracer(SERVICE)


class _LastMessage(logging.Handler):
    """Keeps the OTLP exporter's latest log message for our own export event,
    instead of letting it reach stderr on every batch (B-0005)."""

    def __init__(self) -> None:
        super().__init__()
        self.message: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        self.message = record.getMessage()


_EXPORTER_LOGS = _LastMessage()


class _ReportingExporter(SpanExporter):
    """Logs failed exports at warning, at most once a minute (B-0005).

    Exports run on the batch processor's thread, so a failing or slow backend
    never reaches a request.
    """

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner
        self._reported = -EXPORT_LOG_SECONDS

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        _EXPORTER_LOGS.message = None
        detail: str | None
        try:
            result = self.inner.export(spans)
        except Exception as exc:
            result, detail = SpanExportResult.FAILURE, repr(exc)
        else:
            detail = _EXPORTER_LOGS.message
        if result is SpanExportResult.FAILURE:
            self._report(len(spans), detail)
        return result

    def _report(self, spans: int, detail: str | None) -> None:
        now = time.monotonic()
        if now - self._reported < EXPORT_LOG_SECONDS:
            return
        self._reported = now
        event(log, logging.WARNING, "trace.export_failed", spans=spans, detail=detail)

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def otlp_target(settings: Settings) -> tuple[str, dict[str, str]]:
    """The OTLP traces URL and headers: Langfuse's, with the project keys as
    basic auth, or OTEL_EXPORTER_OTLP_ENDPOINT and its headers when set."""
    if settings.otel_exporter_otlp_endpoint:
        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + TRACES_PATH
    else:
        endpoint = settings.langfuse_host.rstrip("/") + LANGFUSE_PATH
    headers: dict[str, str] = {}
    if settings.otel_exporter_otlp_headers is not None:
        raw = settings.otel_exporter_otlp_headers.get_secret_value()
        headers = dict(parse_env_headers(raw, liberal=True))
    elif settings.langfuse_public_key and settings.langfuse_secret_key:
        pair = (
            f"{settings.langfuse_public_key.get_secret_value()}:"
            f"{settings.langfuse_secret_key.get_secret_value()}"
        )
        headers = {"Authorization": "Basic " + base64.b64encode(pair.encode()).decode()}
    return endpoint, headers


class Tracing:
    """What `configure_tracing` set up; `shutdown` undoes it."""

    def __init__(self, provider: TracerProvider | None) -> None:
        self.provider = provider

    def force_flush(self) -> None:
        if self.provider is not None:
            self.provider.force_flush()

    def shutdown(self) -> None:
        """Export what is queued and stop tracing."""
        if self.provider is None:
            return
        LangChainInstrumentor().uninstrument()
        PROVIDER.current, PROVIDER.content = trace.NoOpTracerProvider(), False
        self.provider.shutdown()
        self.provider = None


def configure_tracing(
    settings: Settings, exporter: SpanExporter | None = None
) -> Tracing:
    """Start tracing if TRACING_ENABLED (B-0001): spans are exported in batches
    (B-0005), and LangChain calls are traced with their content unless
    TRACE_CONTENT is off (B-0004). Tests pass their own `exporter`."""
    if not settings.tracing_enabled:
        return Tracing(None)
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}))
    if exporter is None:
        endpoint, headers = otlp_target(settings)
        exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
    exporter = _ReportingExporter(exporter)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    hide = not settings.trace_content
    instrumentor.instrument(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=hide, hide_outputs=hide),
    )
    exporter_log = logging.getLogger("opentelemetry.exporter")
    exporter_log.propagate = False
    if _EXPORTER_LOGS not in exporter_log.handlers:
        exporter_log.addHandler(_EXPORTER_LOGS)
    PROVIDER.current, PROVIDER.content = provider, settings.trace_content
    return Tracing(provider)


def instrument_app(app: FastAPI) -> None:
    """A server span for every request (B-0002). Done once, at import, since
    middleware can't be added after the app has started."""
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=PROVIDER,
        excluded_urls=NOT_TRACED,
        exclude_spans=["receive", "send"],
    )


@contextmanager
def thread_span(
    name: str,
    thread_id: UUID,
    span_kind: str = "CHAIN",
    **attributes: AttributeValue,
) -> Iterator[Span]:
    """One of our spans for work on a thread; the thread is the session, so
    Langfuse groups a conversation's traces together."""
    tags = {SESSION_ID: str(thread_id), "thread": str(thread_id), SPAN_KIND: span_kind}
    with TRACER.start_as_current_span(name, attributes=tags | attributes) as span:
        yield span


def _node_context() -> Context | None:
    """The graph node's span as parent context. OpenInference doesn't make its
    spans current, so without this a span started in a node would hang off
    agent.run instead."""
    span = langchain_span()
    return trace.set_span_in_context(span) if span is not None else None


@contextmanager
def tool_span(name: str | None, call_id: str | None, args: object) -> Iterator[Span]:
    """`agent.tool <name>` for one tool call; `tool_result` completes it."""
    attributes = {SPAN_KIND: "TOOL", "tool.name": name or "", "call_id": call_id or ""}
    with TRACER.start_as_current_span(
        f"agent.tool {name}", _node_context(), attributes=attributes
    ) as span:
        record_content(span, INPUT, args)
        yield span


def tool_result(span: Span, result: BaseModel) -> None:
    """The call's result: `ok`, or its ToolError code with error status."""
    if isinstance(result, ToolError):
        span.set_attribute("result", result.code)
        span.set_status(StatusCode.ERROR, result.message)
    else:
        span.set_attribute("result", "ok")
    record_content(span, OUTPUT, result.model_dump(mode="json"))


@contextmanager
def confirm_span(tool: str) -> Iterator[Span]:
    """`agent.confirm`. The pause for a decision raises through it, which must
    not mark it as failed; the caller sets `decision`."""
    with TRACER.start_as_current_span(
        "agent.confirm",
        _node_context(),
        attributes={SPAN_KIND: "CHAIN", "tool": tool},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        yield span


def record_content(span: Span, key: str, value: Any) -> None:
    """Message text or tool data, only when TRACE_CONTENT is on (B-0004)."""
    if not PROVIDER.content or value is None:
        return
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    span.set_attribute(key, text)


def tag_error(code: str) -> None:
    """Put an API error code on the current span, the request's server span."""
    trace.get_current_span().set_attribute("error.code", code)
