# SPEC-0001: Agent core

Status: reviewed
Requirements: none yet
Design: none yet

## Goal
A FastAPI service exposing one LangGraph agent with three tools. Each run
returns a validated AgentResponse. Conversation state is checkpointed in Redis
so a thread can be continued or resumed after a confirmation pause.

## Inputs / Outputs
### POST /v1/agent/run  (AgentRequest → AgentResponse)
AgentRequest
- thread_id: UUID | null — null starts a new thread.
- message: str | null — 1..4000 chars after strip. Required unless `confirm` is set.
- confirm: bool | null — only valid when the thread is awaiting confirmation.
  Exactly one of `message` or `confirm` must be set.
- max_tool_calls: int, 1..20, default 5.

AgentResponse
- thread_id: UUID
- status: "ok" | "truncated" | "error" | "awaiting_confirmation"
- answer: str | null — set when status="ok"; may hold a partial answer when "truncated".
- tool_calls: list[ToolCallRecord] — calls made in this request, in order.
- pending_action: PendingAction | null — set only when status="awaiting_confirmation".
- error: ErrorInfo | null — set only when status="error".

ToolCallRecord: id, name, input (tool's input model), output (tool's output model) | null,
error (ToolError) | null — exactly one of output/error is set.
ToolError: code ("invalid_input" | "timeout" | "upstream_error" | "rejected" | "internal"), message.
ErrorInfo: code ("model_output_invalid" | "internal"), message (no stack traces).
PendingAction: tool name and its validated input.

HTTP codes: 200 for every completed run, whatever `status`. 422 for invalid
requests. 409 if a run is already in progress on the thread_id. 503 if Redis or
the LLM provider is unreachable.

### Model
Provider-agnostic tool-calling chat model chosen by settings
(`LLM_PROVIDER` = ollama | anthropic | google, `LLM_MODEL`, provider keys).
Ollama is the local default.

### Tools
1. web_search — WebSearchInput{query: str 1..400, max_results: int 1..5 = 3}
   → WebSearchOutput{results: list[{title, url, snippet}]}. Tavily; read-only.
2. fetch_site — FetchSiteInput{url: http(s) URL}
   → FetchSiteOutput{url, text: str ≤1000 chars, truncated: bool}. Read-only.
   Visible text extracted from HTML (or plain text). 10 s timeout, ≤3 redirects,
   rejects non-text content and private/loopback/link-local addresses.
3. add_note — AddNoteInput{text: str 1..500, single line}
   → AddNoteOutput{written: bool, line: str}. Appends
   "<ISO-8601 UTC timestamp>\t<text>\n" to `NOTES_PATH` (default `./data/notes.txt`).
   Side effect: requires human confirmation (B-0007).

## Behavior
- B-0001: Invalid requests return 422 and make no LLM call.
- B-0002: The agent runs at most max_tool_calls tool calls per request. Errored calls
  count. If a model turn requests more calls than remain, none of that batch runs and
  the run ends with status="truncated".
- B-0003: A tool failure (bad input, timeout, upstream error) is returned to the model
  as a ToolError and recorded in tool_calls; it never raises into the graph.
- B-0004: State is checkpointed in Redis after each step. A request with an existing
  thread_id continues that thread. An unknown thread_id starts a new thread with that id.
  Checkpoints expire `CHECKPOINT_TTL_HOURS` (default 24) after the thread's last write;
  each run resets the clock. An expired thread_id behaves like an unknown one.
- B-0005: Every 200 response validates against AgentResponse. If the model's output
  cannot be used (e.g. tool args fail validation twice in a row, or no final answer),
  the run returns status="error" with code "model_output_invalid"; no exception leaks.
- B-0006: The LLM provider is selected by settings; no code change is needed to switch
  between Ollama, Anthropic and Google.
- B-0007: Before add_note runs, the graph interrupts and returns
  status="awaiting_confirmation" with pending_action. A follow-up request with the same
  thread_id and confirm=true runs the tool and continues; confirm=false records a
  ToolError "rejected" and continues. A `message` sent while awaiting confirmation → 422.
  `confirm` sent when nothing is pending → 422.
- B-0008: A run is limited to `RUN_TIMEOUT_SECONDS` (default 60) total; on timeout
  status="error", code "internal", HTTP 200.
- B-0009: Only one run per thread_id at a time. A request for a thread_id with a run
  in progress returns 409 and does not touch that thread's state. The thread is
  released when the run ends, whatever its status (including timeout and error).

## Acceptance tests
All use a scripted fake model; tests needing Redis use the compose service / a fixture.
- AT-0001 (B-0001): Empty or whitespace message → 422 and the fake model is never called.
  -> `tests/test_api.py::test_empty_message_returns_422`
- AT-0002 (B-0001): max_tool_calls=0 or 21 → 422. -> `tests/test_api.py::test_max_tool_calls_bounds`
- AT-0003 (B-0002): Fake model requests one tool → tool_calls has one entry with matching
  input/output, status="ok". -> `tests/test_agent.py::test_single_tool_call_traced`
- AT-0004 (B-0002): Fake model always requests a tool → stops after max_tool_calls,
  status="truncated". -> `tests/test_agent.py::test_tool_cap_truncates`
- AT-0005 (B-0003): Tool raises internally → ToolError in tool_calls, model receives it,
  run finishes status="ok". -> `tests/test_agent.py::test_tool_error_returned_to_model`
- AT-0006 (B-0004): Two requests with one thread_id → second model call sees the first
  exchange. -> `tests/test_checkpoint.py::test_thread_resumes_from_redis`
- AT-0007 (B-0005): Fake model returns malformed tool args twice → status="error",
  code "model_output_invalid", HTTP 200. -> `tests/test_agent.py::test_malformed_output_errors`
- AT-0008 (B-0007): add_note requested → awaiting_confirmation, file unchanged; confirm=true
  → line appended; confirm=false → ToolError "rejected", file unchanged.
  -> `tests/test_confirmation.py::test_add_note_confirm_flow`
- AT-0009 (B-0006): Settings for each provider build a chat model without network calls.
  -> `tests/test_settings.py::test_provider_selection`
- AT-0010 (tools): fetch_site rejects private IPs and truncates to 1000 chars
  (HTTP mocked); web_search maps a mocked Tavily response.
  -> `tests/tools/test_fetch_site.py`, `tests/tools/test_web_search.py`
- AT-0011 (B-0008): With RUN_TIMEOUT_SECONDS=0.1 and a fake model that sleeps past it
  → HTTP 200, status="error", code "internal", no exception leaks.
  -> `tests/test_agent.py::test_run_timeout_errors`
- AT-0012 (B-0009): While a fake model blocks a run on thread T, a second request for T
  → 409; after the first run ends, a new request for T → 200.
  -> `tests/test_api.py::test_concurrent_thread_returns_409`
- AT-0013 (B-0004): After a run, every Redis key for the thread has a TTL > 0 and
  ≤ CHECKPOINT_TTL_HOURS; a second run resets it.
  -> `tests/test_checkpoint.py::test_checkpoint_ttl_set`

## Non-goals
Auth, streaming, multi-agent routing, a UI, production deployment.

## Open questions
None.

Approved dependencies: langgraph, langgraph-checkpoint-redis, langchain-ollama,
langchain-anthropic, langchain-google-genai, tavily-python, httpx, pydantic-settings,
pytest-asyncio.
