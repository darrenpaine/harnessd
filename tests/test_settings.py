"""Settings and model provider selection (SPEC-0001 B-0001, B-0008)."""

from pathlib import Path
from typing import Any

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama
from pydantic import ValidationError

from agent.llm import build_chat_model
from settings import Settings, get_settings


def _settings(**values: Any) -> Settings:
    """Settings from explicit values only, ignoring any local `.env` file."""
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "LLM_REQUESTS_PER_MINUTE",
        "RUN_TIMEOUT_SECONDS",
        "CHECKPOINT_TTL_HOURS",
    ):
        monkeypatch.delenv(name, raising=False)


PROVIDERS: list[tuple[dict[str, Any], type[BaseChatModel]]] = [
    ({"llm_provider": "ollama", "llm_model": "llama3.1"}, ChatOllama),
    (
        {
            "llm_provider": "anthropic",
            "llm_model": "claude-sonnet-5-5",
            "anthropic_api_key": "test-key",
        },
        ChatAnthropic,
    ),
    (
        {
            "llm_provider": "google",
            "llm_model": "gemini-3.5-flash-lite",
            "google_api_key": "test-key",
        },
        ChatGoogleGenerativeAI,
    ),
]


@pytest.mark.spec("SPEC-0001", "B-0001", "AT-0001")
@pytest.mark.parametrize(("values", "expected"), PROVIDERS)
def test_provider_selection(
    values: dict[str, Any], expected: type[BaseChatModel]
) -> None:
    model = build_chat_model(_settings(**values))
    assert isinstance(model, expected)


@pytest.mark.spec("SPEC-0001", "B-0008", "AT-0011")
@pytest.mark.parametrize("values", [values for values, _ in PROVIDERS])
def test_provider_rate_limit(values: dict[str, Any]) -> None:
    paced = build_chat_model(_settings(**values, llm_requests_per_minute=600))
    limiter = paced.rate_limiter
    assert isinstance(limiter, InMemoryRateLimiter)
    assert limiter.requests_per_second == 10  # one call every 0.1 s
    assert limiter.max_bucket_size == 1  # no burst

    assert build_chat_model(_settings(**values)).rate_limiter is None


@pytest.mark.spec("SPEC-0001", "B-0001")
@pytest.mark.parametrize("provider", ["anthropic", "google"])
def test_hosted_provider_requires_key(provider: str) -> None:
    with pytest.raises(ValidationError, match="API_KEY is required"):
        _settings(llm_provider=provider, llm_model="m")


@pytest.mark.spec("SPEC-0001", "B-0001")
def test_empty_key_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(ValidationError, match="API_KEY is required"):
        _settings()


@pytest.mark.spec("SPEC-0001", "B-0001")
def test_unknown_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(llm_provider="openai")


@pytest.mark.spec("SPEC-0001", "B-0006")
@pytest.mark.spec("SPEC-0004", "B-0003")
def test_defaults() -> None:
    s = _settings()
    assert s.llm_provider == "ollama"
    assert s.run_timeout_seconds == 60
    assert s.checkpoint_ttl_hours == 24
    assert s.notes_path == Path("data/notes.txt")


@pytest.mark.spec("SPEC-0001", "B-0001")
def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "KEY-MARKER")
    monkeypatch.setenv("RUN_TIMEOUT_SECONDS", "0.1")
    s = _settings()
    assert s.llm_provider == "anthropic"
    assert s.run_timeout_seconds == 0.1
    assert s.anthropic_api_key is not None
    assert "KEY-MARKER" not in repr(s)


@pytest.mark.spec("SPEC-0001", "B-0006")
@pytest.mark.spec("SPEC-0004", "B-0003")
@pytest.mark.parametrize("field", ["run_timeout_seconds", "checkpoint_ttl_hours"])
def test_durations_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: 0})


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(Path(__file__).parent)  # no .env here
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


@pytest.mark.spec("SPEC-0001", "B-0001")
def test_build_chat_model_rejects_missing_key() -> None:
    unchecked = Settings.model_construct(llm_provider="anthropic", llm_model="m")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is not set"):
        build_chat_model(unchecked)


@pytest.mark.spec("SPEC-0010", "B-0009", "AT-0009")
def test_tracing_settings_validated() -> None:
    assert _settings().tracing_enabled is False
    with pytest.raises(ValidationError, match="TRACING_ENABLED"):
        _settings(tracing_enabled=True)
    with pytest.raises(ValidationError, match="TRACING_ENABLED"):
        _settings(tracing_enabled=True, langfuse_public_key="pk")
    _settings(tracing_enabled=True, langfuse_public_key="pk", langfuse_secret_key="sk")
    _settings(tracing_enabled=True, otel_exporter_otlp_endpoint="http://otel:4318")
