"""Token counts for the run and thread budgets (SPEC-0012 B-0008, B-0009, B-0014)."""

import json
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage

CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Tokens:
    """Tokens one model call used; `estimated` when the provider reported none."""

    count: int
    estimated: bool


def call_tokens(prompt: Sequence[BaseMessage], reply: AIMessage) -> Tokens:
    """Input + output tokens from the reply's usage metadata, or characters ÷ 4
    over the prompt and the reply when there is none (B-0008)."""
    if reply.usage_metadata is not None:
        usage = reply.usage_metadata
        return Tokens(usage["input_tokens"] + usage["output_tokens"], estimated=False)
    chars = sum(_chars(message) for message in [*prompt, reply])
    return Tokens(chars // CHARS_PER_TOKEN, estimated=True)


def output_tokens(reply: AIMessage) -> int:
    """The reply's own size: its output tokens, or its characters ÷ 4 (B-0014)."""
    if reply.usage_metadata is not None:
        return reply.usage_metadata["output_tokens"]
    return _chars(reply) // CHARS_PER_TOKEN


def _chars(message: BaseMessage) -> int:
    chars = len(str(message.text))
    if isinstance(message, AIMessage):
        chars += sum(len(json.dumps(call["args"])) for call in message.tool_calls)
    return chars


def over_budget(
    run_tokens: int, thread_tokens: int, max_run: int, max_thread: int
) -> str | None:
    """The `budget_exceeded` message when no further model call may be made, or
    None (B-0009, B-0014). The thread is checked first, as only compaction or
    deletion clears it."""
    if thread_tokens >= max_thread:
        return (
            f"the thread has used {thread_tokens} tokens since it was last "
            f"compacted; MAX_TOKENS_PER_THREAD is {max_thread}. Compact or delete "
            "the thread."
        )
    if run_tokens >= max_run:
        return f"the run used {run_tokens} tokens; MAX_TOKENS_PER_RUN is {max_run}"
    return None
