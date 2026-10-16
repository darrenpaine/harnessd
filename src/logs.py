"""Log setup and run events (SPEC-0008).

Each event is one line: its name, then `key=value` fields. Lines logged during a
run carry the run's thread id, taken from a context variable the runner sets.
"""

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from settings import Settings

ROOT = "harnessd"
HTTP_LOGGERS = ("httpx", "httpcore")
INPUT_LIMIT = 200

_thread: ContextVar[str | None] = ContextVar("harnessd_thread", default=None)


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
def thread_context(thread_id: str) -> Iterator[None]:
    """Tag every event logged inside the block with `thread_id` (B-0003)."""
    token = _thread.set(thread_id)
    try:
        yield
    finally:
        _thread.reset(token)


def event(
    logger: logging.Logger,
    level: int,
    name: str,
    *,
    exc_info: bool = False,
    **fields: object,
) -> None:
    """Log one event line: `name thread=<id> key=value ...`.

    Fields set to None are left out.
    """
    if not logger.isEnabledFor(level):
        return
    thread = _thread.get()
    if thread is not None and "thread" not in fields:
        fields = {"thread": thread, **fields}
    parts = [name]
    parts += [f"{k}={_value(v)}" for k, v in fields.items() if v is not None]
    logger.log(level, " ".join(parts), exc_info=exc_info)


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
