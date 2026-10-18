"""Log setup and run events (SPEC-0008).

Each event is one line: its name, then `key=value` fields. Lines logged inside a
`log_context` block also carry that block's fields, such as the run's thread id.

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

from settings import Settings

ROOT = "harnessd"
HTTP_LOGGERS = ("httpx", "httpcore")
INPUT_LIMIT = 200

_context: ContextVar[MappingProxyType[str, object]] = ContextVar(
    "harnessd_log_context", default=MappingProxyType({})
)


def get_logger(module: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{module}")


def configure_logging(settings: Settings) -> None:
    """Send `harnessd` logs to stderr at LOG_LEVEL (B-0001, B-0006).

    The handler is added once, so calling this again only changes the levels.
    """
    level = getattr(logging, settings.log_level.upper())
    root = logging.getLogger(ROOT)
    root.setLevel(level)
    root.propagate = False
    if not any(getattr(h, "harnessd", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.harnessd = True  # type: ignore[attr-defined]
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        root.addHandler(handler)
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

    Context fields come first; a field given here replaces one of the same name.
    Fields set to None are left out.
    """
    if not logger.isEnabledFor(level):
        return
    merged = {**_context.get(), **fields}
    kept = {key: value for key, value in merged.items() if value is not None}
    parts = [name, *(f"{key}={_value(value)}" for key, value in kept.items())]
    logger.log(
        level,
        " ".join(parts),
        exc_info=exc_info,
        extra={"event": name, "fields": kept},
    )


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
