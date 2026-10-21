"""Log setup and run events (SPEC-0008, SPEC-0010).

Each event is one line: its name, then `key=value` fields. Lines logged inside a
`log_context` block also carry that block's fields, such as the request and
thread ids, and the trace id of the current span when tracing is on.

The record keeps the event name and fields as attributes (`event`, `fields`) as
well as in the message, so a formatter other than the text one can use them.
"""

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType

from opentelemetry import trace

from settings import Settings

ROOT = "harnessd"
HTTP_LOGGERS = ("httpx", "httpcore")
INPUT_LIMIT = 200
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Field names in a JSON line where they differ from the text line (SPEC-0010).
JSON_NAMES = {"request": "request_id", "trace": "trace_id"}

_context: ContextVar[MappingProxyType[str, object]] = ContextVar(
    "harnessd_log_context", default=MappingProxyType({})
)


def get_logger(module: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{module}")


class JsonFormatter(logging.Formatter):
    """One JSON object per line (SPEC-0010 B-0008): time, level, logger, event,
    then the event's fields. Records not logged by `event` keep their message."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, object] = {
            "time": time.strftime(TIME_FORMAT, time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        fields: dict[str, object] | None = getattr(record, "fields", None)
        if fields is None:
            line["message"] = record.getMessage()
        else:
            line["event"] = getattr(record, "event", None)
            line |= {JSON_NAMES.get(key, key): value for key, value in fields.items()}
        if record.exc_info:
            line["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str, ensure_ascii=False)


def formatter(settings: Settings) -> logging.Formatter:
    if settings.log_format == "json":
        return JsonFormatter()
    text = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt=TIME_FORMAT
    )
    text.converter = time.gmtime
    return text


def configure_logging(settings: Settings) -> None:
    """Send `harnessd` logs to stderr at LOG_LEVEL in LOG_FORMAT (SPEC-0008
    B-0001, B-0006; SPEC-0010 B-0008).

    The handler is added once, so calling this again only changes the levels
    and the format.
    """
    level = getattr(logging, settings.log_level.upper())
    root = logging.getLogger(ROOT)
    root.setLevel(level)
    root.propagate = False
    handler = next((h for h in root.handlers if getattr(h, "harnessd", False)), None)
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        handler.harnessd = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    handler.setFormatter(formatter(settings))
    http_level = logging.DEBUG if level == logging.DEBUG else logging.WARNING
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(http_level)


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Add `fields` to every event logged inside the block, after any fields of
    an enclosing block; the runner adds `thread` (B-0003)."""
    token = _context.set(MappingProxyType({**_context.get(), **fields}))
    try:
        yield
    finally:
        _context.reset(token)


def event(
    logger: logging.Logger,
    level: int,
    name: str,
    *,
    exc_info: bool = False,
    **fields: object,
) -> None:
    """Log one event line: `name thread=<id> key=value ...`.

    Context fields come first, with the trace id after the request id; a field
    given here replaces one of the same name. Fields set to None are left out.
    """
    if not logger.isEnabledFor(level):
        return
    context = dict(_context.get())
    request = context.pop("request", None)
    merged = {"request": request, "trace": trace_id(), **context, **fields}
    kept = {key: value for key, value in merged.items() if value is not None}
    parts = [name, *(f"{key}={_value(value)}" for key, value in kept.items())]
    logger.log(
        level,
        " ".join(parts),
        exc_info=exc_info,
        extra={"event": name, "fields": kept},
    )


def trace_id() -> str | None:
    """The current span's trace id as 32 hex digits, or None when not tracing."""
    span = trace.get_current_span().get_span_context()
    return format(span.trace_id, "032x") if span.is_valid else None


def elapsed_ms(started: float) -> int:
    """Milliseconds since `started`, a `time.perf_counter()` reading."""
    return round((time.perf_counter() - started) * 1000)


def clip(value: object, limit: int = INPUT_LIMIT) -> str:
    """JSON for a debug field, cut to `limit` characters (B-0004)."""
    text = json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _value(value: object) -> str:
    text = str(value).lower() if isinstance(value, bool) else str(value)
    if not text or any(c.isspace() for c in text):
        return json.dumps(text, ensure_ascii=False)
    return text
