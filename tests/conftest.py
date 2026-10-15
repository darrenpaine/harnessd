"""Shared fixtures (SPEC-0005)."""

from pathlib import Path

import pytest

import redis_support
from settings import Settings
from tools.base import ToolContext

SECRET_ENV_VARS = ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "TAVILY_API_KEY")


@pytest.fixture(autouse=True)
def _clear_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may depend on a real API key (SPEC-0005 B-0001)."""
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def redis_url() -> str:
    """URL of a usable Redis; skips locally or fails in CI if there is none."""
    url = redis_support.redis_url()
    problem = redis_support.redis_problem(url)
    if problem is not None:
        redis_support.unavailable(problem, redis_support.running_in_ci())
    return url


async def public_resolver(host: str) -> list[str]:
    """Resolve every host to one public address, without real DNS."""
    return ["93.184.215.14"]


@pytest.fixture
def tool_context(tmp_path: Path) -> ToolContext:
    """Tool context with a temp notes file, no network and no API keys."""
    settings = Settings(_env_file=None, notes_path=tmp_path / "notes" / "notes.txt")  # type: ignore[call-arg]
    return ToolContext(settings=settings, resolve=public_resolver)
