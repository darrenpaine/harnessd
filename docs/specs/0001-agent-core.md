# SPEC-0001: Agent core

Status: draft
Requirements: none yet
Design: none yet

## Goal
The LangGraph agent loop. Given a user message and the thread's saved state, it calls
the model and tools until it has an answer, hits a limit, or needs confirmation, and
produces a run result. The HTTP layer is SPEC-0002, tools are SPEC-0003, and state
and persistence are SPEC-0004.

## Inputs / Outputs
- Input: thread_id, message (or a confirmation decision), max_tool_calls — validated
  by AgentRequest (SPEC-0002).
- Output: a run result with the AgentResponse fields (SPEC-0002): status, answer,
  tool_calls, pending_action, error.
- Tools: the registry in SPEC-0003.

### Model
Provider-agnostic tool-calling chat model chosen by settings
(`LLM_PROVIDER` = ollama | anthropic | google, `LLM_MODEL`, provider keys).
Ollama is the local default. Built by `build_chat_model()` in `src/agent/llm.py`.

## Behavior
- B-0001: The LLM provider is selected by settings; no code change is needed to switch
  between Ollama, Anthropic and Google. Building the model makes no network calls.
- B-0002: The agent runs at most max_tool_calls tool calls per request. Errored calls
  count. If a model turn requests more calls than remain, none of that batch runs and
  the run ends with status="truncated".
- B-0003: A ToolError returned by a tool (SPEC-0003) is passed back to the model as the
  tool result and recorded in tool_calls; the run continues.
- B-0004: If the model's output cannot be used (e.g. tool args fail validation twice in
  a row, or no final answer), the run ends with status="error", code
  "model_output_invalid"; no exception leaks.
- B-0005: Before any tool with `side_effect=true` runs, the graph interrupts and ends
  the run with status="awaiting_confirmation" and pending_action set. Resuming with
  confirm=true runs the tool and continues; confirm=false records ToolError "rejected"
  and continues.
- B-0006: A run is limited to `RUN_TIMEOUT_SECONDS` (default 60) total; on timeout the
  run ends with status="error", code "internal".

## Acceptance tests
All use a scripted fake model (SPEC-0005).
- AT-0001 (B-0001): Settings for each provider build the matching chat model without
  network calls. -> `tests/test_settings.py::test_provider_selection`
- AT-0002 (B-0002): Fake model requests one tool → tool_calls has one entry with
  matching input/output, status="ok". -> `tests/test_agent.py::test_single_tool_call_traced`
- AT-0003 (B-0002): Fake model always requests a tool → stops after max_tool_calls,
  status="truncated". -> `tests/test_agent.py::test_tool_cap_truncates`
- AT-0004 (B-0003): Tool fails → ToolError in tool_calls, the model receives it, run
  finishes status="ok". -> `tests/test_agent.py::test_tool_error_returned_to_model`
- AT-0005 (B-0004): Fake model returns malformed tool args twice → status="error",
  code "model_output_invalid". -> `tests/test_agent.py::test_malformed_output_errors`
- AT-0006 (B-0005): add_note requested → awaiting_confirmation, file unchanged;
  confirm=true → line appended; confirm=false → ToolError "rejected", file unchanged.
  -> `tests/test_confirmation.py::test_add_note_confirm_flow`
- AT-0007 (B-0006): With RUN_TIMEOUT_SECONDS=0.1 and a fake model that sleeps past it
  → status="error", code "internal", no exception leaks.
  -> `tests/test_agent.py::test_run_timeout_errors`

## Non-goals
Multi-agent routing, streaming, model fallback between providers.

## Open questions
None.
