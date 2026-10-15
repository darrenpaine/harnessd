"""Build an agent runner on a scripted model and an in-memory checkpointer."""

import dataclasses
from typing import Any

import httpx
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from agent.graph import build_graph
from agent.runner import AgentRunner, LocalThreadGuard, ThreadGuard
from fakes import ScriptedChatModel
from schemas import AgentRequest, AgentResponse
from tools import TOOLS
from tools.base import ToolContext

PAGE_TEXT = "page text"


def tool_turn(*calls: tuple[str, dict[str, Any]], text: str = "") -> AIMessage:
    """A model reply requesting `calls`, given as (tool name, args) pairs."""
    return AIMessage(
        content=text,
        tool_calls=[
            {"name": name, "args": args, "id": f"call-{name}-{index}"}
            for index, (name, args) in enumerate(calls)
        ],
    )


def fetch(path: str = "") -> tuple[str, dict[str, Any]]:
    return ("fetch_site", {"url": f"https://example.com/{path}"})


def note(text: str) -> tuple[str, dict[str, Any]]:
    return ("add_note", {"text": text})


class Agent:
    """A runner over the real tools, with fetch_site served by a mock transport."""

    def __init__(
        self,
        ctx: ToolContext,
        replies: list[AIMessage],
        *,
        delay: float = 0,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        guard: ThreadGuard | None = None,
    ) -> None:
        self.fetched: list[str] = []
        self.model = ScriptedChatModel(replies=replies, delay=delay)
        self.ctx = dataclasses.replace(ctx, transport=httpx.MockTransport(self._serve))
        graph = build_graph(
            self.model, self.ctx, TOOLS, checkpointer or InMemorySaver()
        )
        self.runner = AgentRunner(graph, ctx.settings, guard or LocalThreadGuard())

    def _serve(self, request: httpx.Request) -> httpx.Response:
        self.fetched.append(str(request.url))
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, text=PAGE_TEXT
        )

    async def run(self, **fields: Any) -> AgentResponse:
        return await self.runner.run(AgentRequest.model_validate(fields))

    def tool_results(self, call: int) -> list[ToolMessage]:
        """Tool results the model received on its `call`-th call (0-based)."""
        return [m for m in self.model.calls[call] if isinstance(m, ToolMessage)]

    def last_input(self, call: int) -> BaseMessage:
        return self.model.calls[call][-1]
