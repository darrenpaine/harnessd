"""A chat model whose every call is counted, priced and added to the day's spend
(SPEC-0012 B-0008, B-0010, B-0011).

`MeteredModel` is the only place a model call is counted and priced. It holds the
model with its provider, name and price, and the day's spend when there is a daily
cost limit. It sets no timeout and logs no call: callers keep their own.
"""

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable

from agent.budgets import BudgetStop
from agent.prices import cost_micro_usd, price_of
from agent.spend import DailySpend
from settings import Settings

CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Tokens:
    """Tokens one model call used; `estimated` when the provider reported none."""

    input: int
    output: int
    estimated: bool

    @property
    def count(self) -> int:
        return self.input + self.output


def call_tokens(prompt: Sequence[BaseMessage], reply: AIMessage) -> Tokens:
    """Input and output tokens from the reply's usage metadata, or characters ÷ 4
    of the prompt and of the reply when there is none (B-0008)."""
    if (usage := reply.usage_metadata) is not None:
        return Tokens(usage["input_tokens"], usage["output_tokens"], estimated=False)
    return Tokens(
        sum(_chars(message) for message in prompt) // CHARS_PER_TOKEN,
        _chars(reply) // CHARS_PER_TOKEN,
        estimated=True,
    )


def _chars(message: BaseMessage) -> int:
    chars = len(str(message.text))
    if isinstance(message, AIMessage):
        chars += sum(len(json.dumps(call["args"])) for call in message.tool_calls)
    return chars


@dataclass(frozen=True)
class MeteredReply:
    """One model call's reply and what it used. `cost_micro_usd` is 0 for an
    unpriced model; `day_stop` is set when the call brought the day's spend to
    the daily cost limit."""

    reply: AIMessage
    tokens: Tokens
    cost_micro_usd: int
    day_stop: BudgetStop | None


class MeteredModel:
    """`model` as `settings` names it (`LLM_PROVIDER`, `LLM_MODEL`), adding each
    call's cost to `spend`. Without a `spend` (no daily cost limit, eval runs) it
    still counts tokens and cost, never stops and stores nothing."""

    def __init__(
        self,
        model: BaseChatModel,
        settings: Settings,
        spend: DailySpend | None = None,
    ) -> None:
        self._model: Runnable[LanguageModelInput, BaseMessage] = model
        self._chat = model
        self._provider = settings.llm_provider
        self._name = settings.llm_model
        self._price = price_of(self._provider, self._name)
        self._spend = spend

    @property
    def priced(self) -> bool:
        """Whether the model has a price, so its cost can be reported."""
        return self._price is not None

    def with_tools(self, schemas: Sequence[dict[str, Any]]) -> MeteredModel:
        """This metered model with tools bound; it shares the price and spend."""
        bound = copy.copy(self)
        bound._model = self._chat.bind_tools(schemas)
        return bound

    async def call(self, prompt: Sequence[BaseMessage]) -> MeteredReply:
        """One model call. Its cost is added to the day's spend before this
        returns. Provider and spend store errors propagate."""
        reply = await self._model.ainvoke(list(prompt))
        assert isinstance(reply, AIMessage)
        tokens = call_tokens(prompt, reply)
        day_stop = None
        if self._spend is not None:
            # Settings ensure every model in use has a price (B-0011).
            if self._price is None:
                raise ValueError(f"no price for {self._provider} {self._name}")
            day_stop = await self._spend.add(self._cost(tokens))
        return MeteredReply(reply, tokens, self._cost(tokens), day_stop)

    async def day_stop(self) -> BudgetStop | None:
        """The stop to end on, before a call, when the day's spend has reached
        the daily cost limit (B-0010)."""
        return await self._spend.stop() if self._spend is not None else None

    async def refuse_if_reached(self) -> None:
        """Raise DailyLimitError when the daily cost limit is reached, logging
        budget.stop (B-0010, B-0013)."""
        if self._spend is not None:
            await self._spend.refuse_if_reached()

    def _cost(self, tokens: Tokens) -> int:
        if self._price is None:
            return 0
        return cost_micro_usd(self._price, tokens.input, tokens.output)
