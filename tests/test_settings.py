"""Settings and model provider selection (SPEC-0001 B-0006)."""

from pathlib import Path
from typing import Any

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
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
        "RUN_TIMEOUT_SECONDS",
        "CHECKPOINT_TTL_HOURS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.spec("SPEC-0001", "B-0006", "AT-0009")
@pytest.mark.parametrize(
    ("values", "expected"),
    [
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
    ],
)
def test_provider_selection(
    values: dict[str, Any], expected: type[BaseChatModel]
) -> None:
    model = build_chat_model(_settings(**values))
    assert isinstance(model, expected)


@pytest.mark.spec("SPEC-0001", "B-0006")
@pytest.mark.parametrize("provider", ["anthropic", "google"])
def test_hosted_provider_requires_key(provider: str) -> None:
    with pytest.raises(ValidationError, match="API_KEY is required"):
        _settings(llm_provider=provider, llm_model="m")


@pytest.mark.spec("SPEC-0001", "B-0006")
def test_empty_key_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(ValidationError, match="API_KEY is required"):
        _settings()


@pytest.mark.spec("SPEC-0001", "B-0006")
def test_unknown_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(llm_provider="openai")


@pytest.mark.spec("SPEC-0001", "B-0004", "B-0008")
def test_defaults() -> None:
    s = _settings()
    assert s.llm_provider == "ollama"
    assert s.run_timeout_seconds == 60
    assert s.checkpoint_ttl_hours == 24
    assert s.notes_path == Path("data/notes.txt")


@pytest.mark.spec("SPEC-0001", "B-0006")
def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("RUN_TIMEOUT_SECONDS", "0.1")
    s = _settings()
    assert s.llm_provider == "anthropic"
    assert s.run_timeout_seconds == 0.1
    assert s.anthropic_api_key is not None
    assert "secret" not in repr(s)


@pytest.mark.spec("SPEC-0001", "B-0004", "B-0008")
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


@pytest.mark.spec("SPEC-0001", "B-0006")
def test_build_chat_model_rejects_missing_key() -> None:
    unchecked = Settings.model_construct(llm_provider="anthropic", llm_model="m")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is not set"):
        build_chat_model(unchecked)
