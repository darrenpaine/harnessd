"""Token counts for the run and thread budgets (SPEC-0012 B-0008, B-0009, B-0014),
and the budget stops they lead to (B-0013)."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage

from logs import audit

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
class RunUsage:
    """What a run's model calls used: tokens, whether any count was estimated,
    and the cost in micro-dollars from the price table (0 when unpriced)."""

    tokens: int = 0
    estimated: bool = False
    cost_micro_usd: int = 0


@dataclass(frozen=True)
class BudgetStop:
    """A budget that was reached: tokens for run and thread, USD for day."""

    scope: Literal["run", "thread", "day"]
    used: int | float
    limit: int | float
    message: str

    def log(self) -> None:
        """budget.stop, an audit event (B-0013)."""
        audit("budget.stop", scope=self.scope, used=self.used, limit=self.limit)


def over_budget(
    run_tokens: int, thread_tokens: int, max_run: int, max_thread: int
) -> BudgetStop | None:
    """The stop when no further model call may be made, or None (B-0009,
    B-0014). The thread is checked first, as only compaction or deletion
    clears it."""
    if thread_tokens >= max_thread:
        message = (
            f"the thread has used {thread_tokens} tokens since it was last "
            f"compacted; MAX_TOKENS_PER_THREAD is {max_thread}. Compact or delete "
            "the thread."
        )
        return BudgetStop("thread", thread_tokens, max_thread, message)
    if run_tokens >= max_run:
        message = f"the run used {run_tokens} tokens; MAX_TOKENS_PER_RUN is {max_run}"
        return BudgetStop("run", run_tokens, max_run, message)
    return None
