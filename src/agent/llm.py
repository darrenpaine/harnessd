"""Build the tool-calling chat model chosen by settings (B-0006)."""

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama
from pydantic import SecretStr

from settings import Settings


def _require(key: SecretStr | None, name: str) -> SecretStr:
    # Settings already enforces this; the check narrows the type for mypy.
    if key is None:
        raise ValueError(f"{name} is not set")
    return key


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Return a chat model for the configured provider. Makes no network calls."""
    match settings.llm_provider:
        case "ollama":
            return ChatOllama(
                model=settings.llm_model, base_url=settings.ollama_base_url
            )
        case "anthropic":
            return ChatAnthropic(
                model_name=settings.llm_model,
                api_key=_require(settings.anthropic_api_key, "ANTHROPIC_API_KEY"),
                timeout=None,
                stop=None,
            )
        case "google":
            return ChatGoogleGenerativeAI(
                model=settings.llm_model,
                google_api_key=_require(settings.google_api_key, "GOOGLE_API_KEY"),
            )
