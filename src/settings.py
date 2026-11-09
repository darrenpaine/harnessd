"""Service configuration, read from environment variables and `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent.prices import price_of

LLMProvider = Literal["ollama", "anthropic", "google", "openai"]
LogLevel = Literal["debug", "info", "warning", "error"]
LogFormat = Literal["text", "json"]
InjectionScreen = Literal["off", "ollama-systemone", "chat"]
InjectionScreenOnError = Literal["withhold", "allow"]


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
    openai_api_key: SecretStr | None = None
    llm_requests_per_minute: int | None = Field(default=None, ge=1)  # B-0008

    # Tools
    tavily_api_key: SecretStr | None = None
    notes_path: Path = Path("data/notes.txt")

    # Guardrails (SPEC-0012 B-0005, B-0006)
    injection_screen: InjectionScreen = "off"
    injection_screen_model: str | None = None
    injection_threshold: float = Field(default=0.8, ge=0, le=1)
    injection_screen_on_error: InjectionScreenOnError = "withhold"
    # Token budgets (SPEC-0012 B-0009, B-0014)
    max_tokens_per_run: int = Field(default=50_000, gt=0)
    max_tokens_per_thread: int = Field(default=200_000, gt=0)
    daily_cost_limit_usd: float | None = Field(default=None, gt=0)  # B-0010

    # Runtime
    redis_url: str = "redis://localhost:6379"
    run_timeout_seconds: float = Field(default=60, gt=0)  # SPEC-0001 B-0006
    checkpoint_ttl_hours: float = Field(default=24, gt=0)  # SPEC-0004 B-0003
    log_level: LogLevel = "info"  # SPEC-0008 B-0001
    log_format: LogFormat = "text"  # SPEC-0010 B-0008

    # Tracing (SPEC-0010)
    tracing_enabled: bool = False
    langfuse_host: str = "http://localhost:5998"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    # Standard OTel variables; when set they replace the Langfuse values.
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_headers: SecretStr | None = None
    trace_content: bool = True

    # Evals (SPEC-0011)
    eval_repeats: int = Field(default=3, ge=1)
    eval_regression_points: float = Field(default=5, ge=0)

    @field_validator("log_level", "log_format", mode="before")
    @classmethod
    def _lower_log_level(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _provider_key_present(self) -> Self:
        if self.llm_provider == "anthropic" and self.anthropic_api_key is None:
            raise ValueError(
                "ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic"
            )
        if self.llm_provider == "google" and self.google_api_key is None:
            raise ValueError("GOOGLE_API_KEY is required when LLM_PROVIDER=google")
        if self.llm_provider == "openai" and self.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        return self

    @model_validator(mode="after")
    def _screen_model_set(self) -> Self:
        """SPEC-0012: a screen that is on needs a model to ask."""
        if self.injection_screen != "off" and self.injection_screen_model is None:
            raise ValueError(
                "INJECTION_SCREEN_MODEL is required when INJECTION_SCREEN is on"
            )
        return self

    @model_validator(mode="after")
    def _run_budget_fits_thread(self) -> Self:
        """SPEC-0012: a run can never use more than its thread may."""
        if self.max_tokens_per_run > self.max_tokens_per_thread:
            raise ValueError(
                "MAX_TOKENS_PER_RUN must not be larger than MAX_TOKENS_PER_THREAD"
            )
        return self

    @model_validator(mode="after")
    def _limited_models_priced(self) -> Self:
        """SPEC-0012 B-0011: with a daily limit, every chat model in use has a
        price, so spend is never silently uncounted."""
        if self.daily_cost_limit_usd is None:
            return self
        models = [self.llm_model]
        if self.injection_screen == "chat" and self.injection_screen_model:
            models.append(self.injection_screen_model)
        for model in models:
            if price_of(self.llm_provider, model) is None:
                raise ValueError(
                    f"DAILY_COST_LIMIT_USD needs a price for {self.llm_provider} "
                    f"{model}; add it to src/agent/prices.py"
                )
        return self

    @model_validator(mode="after")
    def _tracing_target_set(self) -> Self:
        """SPEC-0010 B-0009: tracing needs Langfuse keys or an OTLP endpoint."""
        keys = self.langfuse_public_key and self.langfuse_secret_key
        if self.tracing_enabled and not keys and not self.otel_exporter_otlp_endpoint:
            raise ValueError(
                "TRACING_ENABLED=true needs LANGFUSE_PUBLIC_KEY and "
                "LANGFUSE_SECRET_KEY, or OTEL_EXPORTER_OTLP_ENDPOINT"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Settings shared by the app; injected into handlers as a dependency."""
    return Settings()
