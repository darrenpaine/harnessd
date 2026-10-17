"""GET, DELETE and compact on /v1/threads/{thread_id} (SPEC-0009)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path

from agent.threads import ThreadService
from api.deps import get_threads
from api.errors import BUSY, NOT_FOUND, UNAVAILABLE, error_responses
from schemas import CompactRequest, CompactResponse, ThreadState

router = APIRouter(prefix="/v1/threads", tags=["threads"])

ThreadId = Annotated[UUID, Path(description="The thread's id.")]
Threads = Annotated[ThreadService, Depends(get_threads)]


@router.get(
    "/{thread_id}",
    operation_id="get_thread",
    summary="Read a thread",
    description=(
        "Return the thread's latest saved state: its messages, any summary, and "
        "the action awaiting confirmation. Does not reset the thread's expiry."
    ),
    responses=error_responses({404: NOT_FOUND, 503: UNAVAILABLE}),
)
async def get_thread(thread_id: ThreadId, threads: Threads) -> ThreadState:
    return await threads.read(thread_id)


@router.delete(
    "/{thread_id}",
    status_code=204,
    operation_id="delete_thread",
    summary="Delete a thread",
    description="Remove the thread's saved state. A later run on the id starts anew.",
    responses=error_responses({404: NOT_FOUND, 409: BUSY, 503: UNAVAILABLE}),
)
async def delete_thread(thread_id: ThreadId, threads: Threads) -> None:
    await threads.delete(thread_id)


@router.post(
    "/{thread_id}/compact",
    operation_id="compact_thread",
    summary="Compact a thread",
    description=(
        "Replace all but the last `keep_turns` turns with a model-written summary, "
        "which later runs receive in the system prompt."
    ),
    responses=error_responses(
        {
            404: NOT_FOUND,
            409: "A run is in progress, or the thread is awaiting confirmation.",
            500: "The summary call timed out or returned no text.",
            503: UNAVAILABLE,
        }
    ),
)
async def compact_thread(
    thread_id: ThreadId,
    threads: Threads,
    body: Annotated[CompactRequest | None, Body()] = None,
) -> CompactResponse:
    keep_turns = (body or CompactRequest()).keep_turns
    return await threads.compact(thread_id, keep_turns)
