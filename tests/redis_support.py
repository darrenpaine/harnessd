"""Decide whether Redis tests can run (SPEC-0005 B-0003)."""

import os
from typing import NoReturn

import pytest
import redis

DEFAULT_REDIS_URL = "redis://localhost:6379"
REQUIRED_MODULES = {"search", "ReJSON"}


def redis_url() -> str:
    return os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)


def running_in_ci() -> bool:
    return os.environ.get("CI", "").lower() == "true"


def redis_problem(url: str) -> str | None:
    """Return why Redis at `url` can't be used, or None if it can."""
    client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    try:
        client.ping()
        loaded = {_module_name(m) for m in client.module_list()}
    except redis.RedisError as exc:
        return f"Redis unreachable at {url}: {exc}"
    finally:
        client.close()
    missing = REQUIRED_MODULES - loaded
    if missing:
        return f"Redis at {url} lacks modules {sorted(missing)}; use redis:8.2 or newer"
    return None


def _module_name(module: dict[bytes | str, object]) -> str:
    name = module.get(b"name", module.get("name", ""))
    return name.decode() if isinstance(name, bytes) else str(name)


def unavailable(reason: str, ci: bool) -> NoReturn:
    """Skip locally; fail in CI, where Redis must be present."""
    if ci:
        pytest.fail(reason)
    pytest.skip(reason)
