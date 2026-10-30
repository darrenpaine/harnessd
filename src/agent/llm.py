"""Build the tool-calling chat model chosen by settings (SPEC-0001 B-0001, B-0008)."""

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.rate_limiters import BaseRateLimiter, InMemoryRateLimiter
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama
from pydantic import SecretStr

from settings import Settings


def _require(key: SecretStr | None, name: str) -> SecretStr:
    # Settings already enforces this; the check narrows the type for mypy.
    if key is None:
        raise ValueError(f"{name} is not set")
    return key


def _rate_limiter(settings: Settings) -> BaseRateLimiter | None:
    """B-0008: calls at most LLM_REQUESTS_PER_MINUTE, evenly spaced. Each model
    gets its own limiter, as provider quotas are per model."""
    if settings.llm_requests_per_minute is None:
        return None
    return InMemoryRateLimiter(
        requests_per_second=settings.llm_requests_per_minute / 60,
        check_every_n_seconds=0.05,
        max_bucket_size=1,
    )


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Return a chat model for the configured provider. Makes no network calls."""
    rate_limiter = _rate_limiter(settings)
    match settings.llm_provider:
        case "ollama":
            return ChatOllama(
                model=settings.llm_model,
                base_url=settings.ollama_base_url,
                rate_limiter=rate_limiter,
            )
        case "anthropic":
            return ChatAnthropic(
                model_name=settings.llm_model,
                api_key=_require(settings.anthropic_api_key, "ANTHROPIC_API_KEY"),
                timeout=None,
                stop=None,
                rate_limiter=rate_limiter,
            )
        case "google":
            return ChatGoogleGenerativeAI(
                model=settings.llm_model,
                google_api_key=_require(settings.google_api_key, "GOOGLE_API_KEY"),
                rate_limiter=rate_limiter,
            )
