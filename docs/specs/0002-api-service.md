# SPEC-0002: API service

Status: draft
Requirements: none yet
Design: [design.md](../design/design.md)

## Goal
The FastAPI service around the agent (SPEC-0001): request and response models, HTTP
status codes, the error body format, and the health check.

## Inputs / Outputs
### POST /v1/agent/run  (AgentRequest → AgentResponse)
AgentRequest
- thread_id: UUID | null — null starts a new thread (SPEC-0004).
- message: str | null — 1..4000 chars after strip. Required unless `confirm` is set.
- confirm: bool | null — only valid when the thread is awaiting confirmation.
  Exactly one of `message` or `confirm` must be set.
- max_tool_calls: int, 1..20, default 5.
- Unknown fields are rejected.

AgentResponse
- thread_id: UUID
- status: "ok" | "truncated" | "error" | "awaiting_confirmation"
- answer: str | null — set when status="ok"; may hold a partial answer when
  "truncated"; null otherwise.
- tool_calls: list[ToolCallRecord] (SPEC-0003) — calls made in this request, in order.
- pending_action: PendingAction | null — set only when status="awaiting_confirmation".
  PendingAction: tool name and its validated input.
- error: ErrorInfo | null — set only when status="error".
  ErrorInfo: code ("model_output_invalid" | "internal"), message (no stack traces).

### GET /health
→ 200 `{"status": "ok"}`

### HTTP status codes
| Code | When | Body |
|------|------|------|
| 200 | Every completed run, whatever `status` | AgentResponse |
| 422 | Invalid request | FastAPI's default validation body |
| 409 | A run is already in progress on the thread_id | `{"error": {"code": "thread_busy", "message": ...}}` |
| 503 | Redis or the LLM provider is unreachable, or the provider rejects the request (B-0007) | `{"error": {"code": "dependency_unavailable", "message": ...}}` |
| 500 | Any other unexpected error (B-0008) | `{"error": {"code": "internal", "message": ...}}` |

The compiled graph is injected into handlers with a FastAPI dependency, so tests can
override it.

Swagger UI, ReDoc and the OpenAPI schema are specified in SPEC-0007.

## Behavior
- B-0001: Invalid requests return 422 and make no LLM call.
- B-0002: Every 200 response validates against AgentResponse; fields that don't match
  `status` (e.g. status="ok" without an answer) cannot be returned.
- B-0003: A `message` sent while the thread is awaiting confirmation → 422. `confirm`
  sent when nothing is pending → 422.
- B-0004: A request for a thread_id with a run in progress (SPEC-0004 B-0005) returns
  409 `thread_busy` and does not touch that thread's state.
- B-0005: If Redis or the LLM provider is unreachable, the request returns 503
  `dependency_unavailable`; no stack trace is returned.
- B-0006: `GET /health` returns 200 `{"status": "ok"}` without calling Redis or the LLM.
- B-0007: If the LLM provider is reachable but rejects the request for a reason the
  client cannot fix (the model is not found, the API key is invalid or lacks
  permission, the provider rate-limits the request, or another provider API error),
  the request returns 503 `dependency_unavailable`. The message names the cause and
  the model (e.g. "the LLM provider has no model 'llama9'"); it contains no API key and
  no stack trace.
- B-0008: Any other unexpected exception returns 500 with code `internal` and a fixed
  message; no stack trace or exception text is returned. The exception is logged.

## Acceptance tests
- AT-0001 (B-0001): Empty or whitespace message → 422 and the fake model is never called.
  -> `tests/test_api.py::test_empty_message_returns_422`
- AT-0002 (B-0001): max_tool_calls=0 or 21 → 422.
  -> `tests/test_api.py::test_max_tool_calls_bounds`
- AT-0003 (B-0002): Each status with mismatched fields fails AgentResponse validation.
  -> `tests/test_schemas.py::test_response_rejects_inconsistent_status`
- AT-0004 (B-0003): message while awaiting confirmation → 422; confirm with nothing
  pending → 422. -> `tests/test_api.py::test_confirm_state_rules`
- AT-0005 (B-0004): While a fake model blocks a run on thread T, a second request for T
  → 409 `thread_busy`; after the first run ends, a new request for T → 200.
  -> `tests/test_api.py::test_concurrent_thread_returns_409`
- AT-0006 (B-0005): Graph dependency raising a Redis connection error → 503
  `dependency_unavailable`. -> `tests/test_api.py::test_dependency_unavailable_returns_503`
- AT-0007 (B-0006): `GET /health` → 200 `{"status": "ok"}`.
  -> `tests/test_health.py::test_health_returns_ok`
- AT-0008 (B-0007): The model raises Ollama's 404 "model not found", and separately
  LangChain's not-found, authentication and rate-limit errors → 503
  `dependency_unavailable`; the message names the cause; the key is not in the body.
  -> `tests/test_api.py::test_provider_rejection_returns_503`
- AT-0009 (B-0008): The graph raises an unexpected exception → 500
  `{"error": {"code": "internal", ...}}` without the exception text.
  -> `tests/test_api.py::test_unexpected_error_returns_500`

## Non-goals
Auth, a UI, rate limiting. Streaming (SSE) comes in a later spec.

## Open questions
None.
