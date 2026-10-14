"""Test doubles shared across the suite (SPEC-0005)."""

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool


class ScriptedChatModel(BaseChatModel):
    """Chat model that replays scripted replies in order (SPEC-0005 B-0002).

    Every call's input messages are recorded in `calls`. Calling it more times
    than there are scripted replies fails the test.
    """

    replies: list[AIMessage]
    calls: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if len(self.calls) >= len(self.replies):
            raise AssertionError(
                f"ScriptedChatModel called {len(self.calls) + 1} times; "
                f"only {len(self.replies)} replies scripted"
            )
        self.calls.append(list(messages))
        reply = self.replies[len(self.calls) - 1]
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        # Tool calls come from the script, so binding tools changes nothing.
        return self
