# SPEC-0009: Thread management

Status: draft
Requirements: none yet
Design: [design.md](../design/design.md)

## Goal
Let a client read a thread's saved state, delete a thread, and compact a long thread
by replacing its older messages with a model-written summary, so the history sent to
the model and the size of the thread's Redis keys stay bounded.

## Inputs / Outputs
All three take `thread_id` as a UUID path parameter (anything else → 422). They are
documented like the other operations (SPEC-0007 B-0003, B-0004) under the tag
`threads`, and `docs/openapi.json` is regenerated.

| Endpoint | operationId | Success | Errors |
|----------|-------------|---------|--------|
| `GET /v1/threads/{thread_id}` | `get_thread` | 200 ThreadState | 404, 422, 503 |
| `DELETE /v1/threads/{thread_id}` | `delete_thread` | 204, no body | 404, 409, 422, 503 |
| `POST /v1/threads/{thread_id}/compact` | `compact_thread` | 200 CompactResponse | 404, 409, 422, 503, 500 |

404 and 409 use the ApiError body (SPEC-0002) with codes `thread_not_found`,
`thread_busy` and `thread_awaiting_confirmation`; the first and last are new
ApiErrorCode values. 503 and 500 are as in SPEC-0002 B-0005, B-0007 and B-0008.

### ThreadState
- thread_id: UUID
- status: `"idle"` | `"awaiting_confirmation"`
- pending_action: PendingAction | null — set only when awaiting confirmation.
- summary: str | null — the summary written by the last compaction, if any.
- messages: list[ThreadMessage], oldest first, after the summary.
- expires_in_seconds: int | null — seconds until the thread's keys expire
  (SPEC-0004 B-0003); null when the store does not expire.

ThreadMessage
- role: `"user"` | `"assistant"` | `"tool"`
- content: str — text only.
- tool_calls: list[{id, name, args}] — the calls an assistant message requested,
  as the model sent them (args may be invalid); empty for other roles.
- tool_call_id: str | null — set only on `tool` messages.

The system prompt and the run counters (calls used, queued calls) are not returned.

### CompactRequest (optional body)
- keep_turns: int, 0..20, default 2 — the most recent turns to keep word for word.
  A turn is a user message and everything after it up to the next user message.

### CompactResponse
- thread_id: UUID
- compacted: bool — false when there was nothing to compact.
- messages_removed: int
- summary: str | null — the thread's summary after the call.

## Behavior
- B-0001: `GET` returns the thread's latest saved state as ThreadState. It does not
  take the thread lock, change the state, or reset the TTL. An unknown or expired
  thread → 404 `thread_not_found`.
- B-0002: `DELETE` removes every checkpoint key for the thread and returns 204. It
  holds the thread lock while it runs (B-0003) and releases it after. A later run with the same id starts a new
  thread (SPEC-0004 B-0002). An unknown or expired thread → 404.
- B-0003: `DELETE` and `compact` take the thread lock (SPEC-0004 B-0005); while a run
  holds it they return 409 `thread_busy` and change nothing. A run started during a
  compaction gets 409.
- B-0004: `compact` asks the configured chat model to summarise every message
  before the last `keep_turns` turns, together with any earlier summary. It then
  removes those messages from the state, stores the result as `summary`, and
  resets the TTL (SPEC-0004 B-0003). A tool call and its results are never split:
  removal stops at a turn boundary.
- B-0005: On later runs the summary is sent to the model as part of the system
  prompt ("Summary of the earlier conversation: …"), never as a user or assistant
  message, so the history stays valid for every provider.
- B-0006: If the thread has no more than `keep_turns` turns, `compact` makes no
  model call and returns 200 with compacted=false and the state unchanged.
- B-0007: A thread awaiting confirmation cannot be compacted: 409 with code
  `thread_awaiting_confirmation`. (Removing the paused call's turn would lose the
  call the confirmation resumes.)
- B-0008: The summarising call is limited by `RUN_TIMEOUT_SECONDS`. If it times out,
  fails or returns no text, the state is unchanged; a timeout or empty reply → 500
  `internal`, a provider error → 503 as in SPEC-0002.
- B-0009: Each endpoint logs one info event (SPEC-0008): `thread.read`,
  `thread.deleted`, `thread.compacted` (messages_removed, ms).

## Acceptance tests
Unit tests use the in-memory graph and a scripted fake model; tests named `redis`
use real Redis (SPEC-0005).
- AT-0001 (B-0001): After a run with one web_search call, GET returns status idle and
  messages user, assistant (one tool call), tool, assistant; a paused add_note run
  returns awaiting_confirmation with its pending_action.
  -> `tests/test_threads.py::test_get_thread_state`
- AT-0002 (B-0001): GET for an unknown id → 404 `thread_not_found`; an invalid id → 422.
  -> `tests/test_threads.py::test_get_unknown_thread`
- AT-0003 (B-0001): GET leaves the key TTLs unchanged and expires_in_seconds is
  within CHECKPOINT_TTL_HOURS. -> `tests/test_threads.py::test_get_does_not_touch_ttl` (redis)
- AT-0004 (B-0002): After DELETE → 204, no Redis key containing the thread id remains,
  GET → 404, and a new run on the id sees only its own message.
  -> `tests/test_threads.py::test_delete_thread` (redis)
- AT-0005 (B-0003): While a fake model blocks a run, DELETE and compact → 409
  `thread_busy` and the state is unchanged. -> `tests/test_threads.py::test_busy_thread_409`
- AT-0006 (B-0004, B-0005): A thread with 4 turns, compact with keep_turns=1 →
  compacted=true, 3 turns' messages removed, GET shows the summary and the last
  turn; the next run's model call gets a system prompt containing the summary and
  no removed message. -> `tests/test_threads.py::test_compact_thread`
- AT-0007 (B-0004): Compacting twice → the second summary call receives the first
  summary. -> `tests/test_threads.py::test_compact_includes_previous_summary`
- AT-0008 (B-0006): A 1-turn thread with keep_turns=2 → compacted=false, the model is
  not called. -> `tests/test_threads.py::test_compact_nothing_to_do`
- AT-0009 (B-0007): compact on a thread awaiting confirmation → 409
  `thread_awaiting_confirmation`. -> `tests/test_threads.py::test_compact_awaiting_confirmation`
- AT-0010 (B-0008): The summary model times out, raises a provider error, or returns
  empty text → 500 / 503 / 500 and GET shows the state unchanged.
  -> `tests/test_threads.py::test_compact_failure_leaves_state`
- AT-0011 (B-0001..B-0008): The OpenAPI schema lists the three operations with the
  documented responses. -> `tests/test_api_docs.py::test_operations_documented`

## Non-goals
Listing threads, automatic compaction, editing or deleting single messages, undoing
a compaction, restoring a deleted thread.

## Open questions
None. Settled in review: REST paths, 404 for deleting an unknown thread,
keep_turns default 2.
