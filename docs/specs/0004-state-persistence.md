# SPEC-0004: State and persistence

Status: implemented
Requirements: R-0005, R-0006, R-0013
Design: [design.md](../design/design.md)

## Goal
How conversation state is identified, saved in Redis, expired and resumed, and how
concurrent runs on one thread are prevented.

## Inputs / Outputs
- thread_id: UUID. If a request has none, the service generates one with uuid4 and
  returns it in AgentResponse (SPEC-0002).
- Storage: LangGraph Redis checkpointer (`langgraph-checkpoint-redis`) at `REDIS_URL`.
  Checkpoint keys are the checkpointer's own; the thread lock key is
  `harnessd:lock:thread:<thread_id>`.
- Settings: `CHECKPOINT_TTL_HOURS` (default 24), `RUN_TIMEOUT_SECONDS` (SPEC-0001 B-0006).

## Behavior
- B-0001: State is checkpointed in Redis after each graph step. A request with an
  existing thread_id continues that thread.
- B-0002: An unknown or expired thread_id starts a new thread with that id.
- B-0003: Checkpoints expire `CHECKPOINT_TTL_HOURS` after the thread's last write;
  each run resets the clock.
- B-0004: A pending confirmation (SPEC-0001 B-0005) is part of the checkpoint, so it
  survives across requests and an app restart.
- B-0005: Only one run per thread_id at a time, enforced by a Redis lock so it holds
  across workers. The lock is released when the run ends, whatever its status, and
  expires on its own after `RUN_TIMEOUT_SECONDS` + 30 s so a crashed run cannot block a
  thread forever.
- B-0006: A thread can pause for confirmation any number of times. Loading a
  thread uses only the pending writes saved with its latest checkpoint, never
  those left over from earlier steps, so each confirmation behaves like the first.

## Acceptance tests
All use real Redis (SPEC-0005), never a checkpointer mock.
- AT-0001 (B-0001): Two requests with one thread_id → the second model call sees the
  first exchange. -> `tests/test_checkpoint.py::test_thread_resumes_from_redis`
- AT-0002 (B-0002): A request with a new thread_id → the model sees only that message.
  -> `tests/test_checkpoint.py::test_unknown_thread_starts_fresh`
- AT-0003 (B-0003): After a run, every Redis key for the thread has a TTL > 0 and
  ≤ CHECKPOINT_TTL_HOURS; a second run resets it.
  -> `tests/test_checkpoint.py::test_checkpoint_ttl_set`
- AT-0004 (B-0004): Run to awaiting_confirmation, build a new graph on the same Redis,
  confirm → the tool runs. -> `tests/test_checkpoint.py::test_pending_confirmation_survives_restart`
- AT-0005 (B-0005): Lock is released after ok, error and timeout runs, and has an
  expiry while held. -> `tests/test_checkpoint.py::test_thread_lock_released`
- AT-0006 (B-0006): On one thread, three messages that each make an add_note call,
  answered with confirm true, confirm true and confirm false → each message returns
  awaiting_confirmation, each confirm returns ok, two notes are written, the third
  call's result is `rejected`, and GET (SPEC-0009) succeeds after every request.
  -> `tests/test_checkpoint.py::test_repeated_confirmations`

## Non-goals
Listing threads, migrating checkpoint formats. Reading, deleting and compacting
threads through the API are in SPEC-0009.

## Open questions
None.
