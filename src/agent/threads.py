"""Read, delete and compact a thread's saved state (SPEC-0009).

The API calls ThreadService; like the runner it takes the thread lock for
anything that changes a thread, and raises for the API to map to a status code.
"""

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import StateSnapshot

from agent.graph import AgentGraph, thread_config
from agent.persistence import ThreadGuard
from logs import elapsed_ms, event, get_logger, log_context
from schemas import (
    CompactResponse,
    PendingAction,
    ThreadMessage,
    ThreadState,
    ThreadToolCall,
)
from settings import Settings
from tracing import thread_span

SUMMARY_PROMPT = (
    "Summarise the conversation below for an assistant that will continue it. "
    "Keep the facts, decisions, open questions and tool results it may need; "
    "fold in the earlier summary if there is one. Reply with the summary only."
)

log = get_logger("threads")


class ThreadNotFoundError(Exception):
    """No saved state for the thread: unknown, expired or deleted (B-0001)."""


class ThreadAwaitingConfirmationError(Exception):
    """A paused thread cannot be compacted (B-0007)."""


class ThreadRunUnfinishedError(Exception):
    """The last run stopped part way, with graph steps pending (B-0011)."""


class SummaryError(Exception):
    """The summary call timed out or returned no text (B-0008)."""


class ThreadService:
    def __init__(
        self,
        graph: AgentGraph,
        model: BaseChatModel,
        settings: Settings,
        guard: ThreadGuard,
    ) -> None:
        self.graph = graph
        self.model = model
        self.settings = settings
        self.guard = guard

    async def read(self, thread_id: UUID) -> ThreadState:
        """B-0001: the latest state, without the lock or a TTL reset."""
        with log_context(thread=str(thread_id)), thread_span("thread.read", thread_id):
            snapshot = await self._snapshot(thread_id)
            pending = snapshot.interrupts
            state = ThreadState(
                thread_id=thread_id,
                status="awaiting_confirmation" if pending else "idle",
                pending_action=(
                    PendingAction.model_validate(pending[0].value) if pending else None
                ),
                summary=snapshot.values.get("summary"),
                messages=[thread_message(m) for m in snapshot.values["messages"]],
                expires_in_seconds=await self.guard.expires_in(str(thread_id)),
            )
            event(log, logging.INFO, "thread.read")
            return state

    async def delete(self, thread_id: UUID) -> None:
        """B-0002: remove every checkpoint of the thread, under its lock."""
        with (
            log_context(thread=str(thread_id)),
            thread_span("thread.delete", thread_id),
        ):
            async with self.guard.hold(str(thread_id)):
                await self._snapshot(thread_id)
                saver = self.graph.checkpointer
                assert isinstance(saver, BaseCheckpointSaver)
                await saver.adelete_thread(str(thread_id))
            event(log, logging.INFO, "thread.deleted")

    async def compact(self, thread_id: UUID, keep_turns: int) -> CompactResponse:
        """B-0004, B-0006, B-0007, B-0011: replace all but the last `keep_turns`
        turns with a summary. The state changes only once the summary is written.

        A thread with pending steps is refused: rewriting its state would leave
        those steps behind, and the update would lose the summary.
        """
        with (
            log_context(thread=str(thread_id)),
            thread_span("thread.compact", thread_id, keep_turns=keep_turns) as span,
        ):
            started = time.perf_counter()
            async with self.guard.hold(str(thread_id)):
                snapshot = await self._snapshot(thread_id)
                if snapshot.interrupts:
                    raise ThreadAwaitingConfirmationError(str(thread_id))
                if snapshot.next:
                    raise ThreadRunUnfinishedError(str(thread_id))
                messages: list[BaseMessage] = snapshot.values["messages"]
                summary: str | None = snapshot.values.get("summary")
                removed = messages[: compaction_cut(messages, keep_turns)]
                if removed:
                    summary = await self._summarise(summary, removed)
                    update = {
                        "messages": [RemoveMessage(id=m.id or "") for m in removed],
                        "summary": summary,
                    }
                    await self.graph.aupdate_state(thread_config(thread_id), update)
                    await self.guard.touch(str(thread_id))
            event(
                log,
                logging.INFO,
                "thread.compacted",
                messages_removed=len(removed),
                ms=elapsed_ms(started),
            )
            span.set_attribute("messages_removed", len(removed))
            return CompactResponse(
                thread_id=thread_id,
                compacted=bool(removed),
                messages_removed=len(removed),
                summary=summary,
            )

    async def _snapshot(self, thread_id: UUID) -> StateSnapshot:
        snapshot = await self.graph.aget_state(thread_config(thread_id))
        if not snapshot.values:
            raise ThreadNotFoundError(str(thread_id))
        return snapshot

    async def _summarise(
        self, previous: str | None, messages: Sequence[BaseMessage]
    ) -> str:
        """B-0004, B-0008: one model call under the run timeout. Provider
        errors propagate for the API to map to 503."""
        prompt = [
            SystemMessage(content=SUMMARY_PROMPT),
            HumanMessage(content=transcript(previous, messages)),
        ]
        limit = self.settings.run_timeout_seconds
        try:
            async with asyncio.timeout(limit):
                reply = await self.model.ainvoke(prompt)
        except TimeoutError as exc:
            _summary_failed("timeout")
            raise SummaryError(f"the summary took longer than {limit:g} s") from exc
        text = str(reply.text).strip()
        if not text:
            _summary_failed("empty")
            raise SummaryError("the model returned an empty summary")
        return text


def _summary_failed(reason: str) -> None:
    """thread.compact_failed (B-0008, SPEC-0008)."""
    event(log, logging.WARNING, "thread.compact_failed", reason=reason)


def compaction_cut(messages: Sequence[BaseMessage], keep_turns: int) -> int:
    """How many leading messages to remove so `keep_turns` turns remain.

    A turn starts at a user message, so a tool call and its results are never
    split. Returns 0 when there are no more than `keep_turns` turns (B-0006).
    """
    starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(starts) <= keep_turns:
        return 0
    return starts[-keep_turns] if keep_turns else len(messages)


def thread_message(message: BaseMessage) -> ThreadMessage:
    text = str(message.text)
    if isinstance(message, AIMessage):
        calls = [*message.tool_calls, *message.invalid_tool_calls]
        return ThreadMessage(
            role="assistant",
            content=text,
            tool_calls=[
                ThreadToolCall(id=c["id"], name=c["name"], args=c["args"])
                for c in calls
            ],
        )
    if isinstance(message, ToolMessage):
        return ThreadMessage(
            role="tool", content=text, tool_call_id=message.tool_call_id
        )
    return ThreadMessage(role="user", content=text)


def transcript(previous: str | None, messages: Sequence[BaseMessage]) -> str:
    """The text the summary call is asked to summarise."""
    lines = [f"Earlier summary: {previous}"] if previous else []
    for message in map(thread_message, messages):
        if message.content:
            lines.append(f"{message.role}: {message.content}")
        for call in message.tool_calls:
            args = json.dumps(call.args, ensure_ascii=False)
            lines.append(f"{message.role} called {call.name} with {args}")
    return "\n".join(lines)
