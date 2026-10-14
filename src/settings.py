"""Service configuration, read from environment variables and `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["ollama", "anthropic", "google"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore"
    )

    # Model (SPEC-0001 B-0001)
    llm_provider: LLMProvider = "ollama"
    llm_model: str = "llama3.1"
    ollama_base_url: str = "http://localhost:11434"
    anthropic_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None

    # Tools
    tavily_api_key: SecretStr | None = None
    notes_path: Path = Path("data/notes.txt")

    # Runtime
    redis_url: str = "redis://localhost:6379"
    run_timeout_seconds: float = Field(default=60, gt=0)  # SPEC-0001 B-0006
    checkpoint_ttl_hours: float = Field(default=24, gt=0)  # SPEC-0004 B-0003

    @model_validator(mode="after")
    def _provider_key_present(self) -> Self:
        if self.llm_provider == "anthropic" and self.anthropic_api_key is None:
            raise ValueError(
                "ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic"
            )
        if self.llm_provider == "google" and self.google_api_key is None:
            raise ValueError("GOOGLE_API_KEY is required when LLM_PROVIDER=google")
        return self


@lru_cache
def get_settings() -> Settings:
    """Settings shared by the app; injected into handlers as a dependency."""
    return Settings()
