"""The injection screen: how likely it is that text tries to direct an AI agent
that reads it (SPEC-0012 B-0005).

A `Screen` raises on any failure, including invalid output and timeouts; the
caller decides what a failure means (B-0006). Nothing here retries. `preload`
loads a System One model ahead of the first screened result (B-0017).
"""

import asyncio
import logging
from typing import Protocol

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from agent.llm import build_chat_model
from agent.metered import MeteredModel
from agent.spend import DailySpend
from logs import event, get_logger
from prompts import INJECTION_SCREEN_CHAT_PROMPT, INJECTION_SCREEN_PROMPT
from schemas import (
    ChatScreenReply,
    SystemOneQuestion,
    SystemOneQuestions,
    SystemOneReply,
    SystemOneRequest,
)
from settings import Settings

# A warm screen answers in seconds. Not a setting: it only has to stay well under
# RUN_TIMEOUT_SECONDS.
TIMEOUT_SECONDS = 20.0
# A cold load of nimble takes one to two minutes.
PRELOAD_TIMEOUT_SECONDS = 300.0

log = get_logger("screen")


class Screen(Protocol):
    async def probability(self, text: str) -> float:
        """The probability that `text` is an injection; raises on failure."""
        ...


class SystemOneScreen:
    """Asks a System One decision model on Ollama one `noul` question."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/systemone"
        self._model = model
        self._timeout = timeout_seconds
        self._transport = transport

    async def probability(self, text: str) -> float:
        return await self._ask(text, self._timeout)

    async def preload(self) -> None:
        """Load the model, waiting as long as a cold load takes."""
        await self._ask("ready", PRELOAD_TIMEOUT_SECONDS)

    async def _ask(self, text: str, timeout: float) -> float:
        question = SystemOneQuestion(instructions=INJECTION_SCREEN_PROMPT)
        request = SystemOneRequest(
            model=self._model,
            state=text,
            questions=SystemOneQuestions(injection=question),
        )
        async with httpx.AsyncClient(
            transport=self._transport, timeout=timeout
        ) as client:
            response = await client.post(self._url, json=request.model_dump())
        response.raise_for_status()
        reply = SystemOneReply.model_validate_json(response.content)
        return reply.answers.injection.noul


class ChatScreen:
    """Asks a chat model for the probability as JSON."""

    def __init__(
        self,
        model: MeteredModel,
        *,
        timeout_seconds: float = TIMEOUT_SECONDS,
    ) -> None:
        self._model = model
        self._timeout = timeout_seconds

    async def probability(self, text: str) -> float:
        prompt = f"{INJECTION_SCREEN_PROMPT}\n\n{INJECTION_SCREEN_CHAT_PROMPT}"
        messages = [SystemMessage(content=prompt), HumanMessage(content=text)]
        async with asyncio.timeout(self._timeout):
            result = await self._model.call(messages)  # SPEC-0012 B-0010
        return ChatScreenReply.model_validate_json(str(result.reply.text)).probability


async def preload(screen: Screen | None) -> None:
    """Load a System One screen's model (B-0017). Other screens need no load. A
    failure is logged, never raised: B-0006 covers calls made before it is ready."""
    if not isinstance(screen, SystemOneScreen):
        return
    try:
        await screen.preload()
    except Exception as exc:
        event(log, logging.WARNING, "screen.preload_failed", error=type(exc).__name__)


def build_screen(settings: Settings, spend: DailySpend | None = None) -> Screen | None:
    """The configured screen, or None when it is off. Makes no network calls. A
    `chat` screen adds its calls to `spend` (SPEC-0012 B-0010)."""
    model = settings.injection_screen_model
    if settings.injection_screen == "off":
        return None
    if model is None:  # Settings already enforces this; narrows the type for mypy.
        raise ValueError("INJECTION_SCREEN_MODEL is not set")
    match settings.injection_screen:
        case "ollama-systemone":
            return SystemOneScreen(settings.ollama_base_url, model)
        case "chat":
            chat = settings.model_copy(update={"llm_model": model})
            return ChatScreen(MeteredModel(build_chat_model(chat), chat, spend))
