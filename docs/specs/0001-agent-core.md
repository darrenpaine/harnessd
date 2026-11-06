# SPEC-0001: Agent core

Status: draft
Requirements: R-0007, R-0008, R-0025
Design: [design.md](../design/design.md)

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
(`LLM_PROVIDER` = ollama | anthropic | google | openai, `LLM_MODEL`, provider keys:
`ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`).
Ollama is the local default. Built by `build_chat_model()` in `src/agent/llm.py`.
`LLM_REQUESTS_PER_MINUTE` (int ≥ 1, unset by default) paces the model's calls to
fit a provider quota, such as a free tier's requests-per-minute limit (B-0008).

## Behavior
- B-0001: The LLM provider is selected by settings; no code change is needed to switch
  between Ollama, Anthropic, Google and OpenAI. `LLM_PROVIDER=anthropic` requires
  `ANTHROPIC_API_KEY`, `google` requires `GOOGLE_API_KEY` and `openai` requires
  `OPENAI_API_KEY`; a missing or empty key fails settings at startup. The OpenAI
  model always calls OpenAI's own API: `OPENAI_API_BASE` and `OPENAI_BASE_URL` in
  the environment are ignored, so they can't send calls and the key elsewhere.
  Building the model makes no network calls.
- B-0002: The agent runs at most max_tool_calls tool calls per request. Errored calls
  count. If a model turn requests more calls than remain, none of that batch runs and
  the run ends with status="truncated". `answer` then holds the text of that model
  turn, or null if it had none.
- B-0003: A ToolError returned by a tool (SPEC-0003) is passed back to the model as the
  tool result and recorded in tool_calls; the run continues.
- B-0004: If the model's output cannot be used (e.g. tool args fail validation twice in
  a row, or no final answer), the run ends with status="error", code
  "model_output_invalid"; no exception leaks. A call that names an unknown tool or
  whose args fail the tool's Input model is returned to the model as ToolError
  `invalid_input` and counts toward max_tool_calls. It is not added to tool_calls,
  because a ToolCallRecord holds only valid input. If the next model turn also has
  such a call, none of that turn's calls run and the run ends with this error. A
  reply with no tool calls and no text also counts as no final answer.
- B-0005: Before any tool with `side_effect=true` runs, the graph interrupts and ends
  the run with status="awaiting_confirmation" and pending_action set. Resuming with
  confirm=true runs the tool and continues; confirm=false records ToolError "rejected"
  and continues. The calls in one model turn run in order: calls before a
  side-effecting call run first, and calls after it run once it is confirmed or
  rejected. Each side-effecting call is confirmed separately.
- B-0006: A run is limited to `RUN_TIMEOUT_SECONDS` (default 60) total; on timeout the
  run ends with status="error", code "internal".
- B-0007: A thread stays usable after a truncated or timed-out run. On the next run,
  tool calls from earlier turns that never ran are shown to the model as ToolError
  `internal` ("not run"), so the history the provider sees is always well formed.

- B-0008: With `LLM_REQUESTS_PER_MINUTE` = N, each model built by
  `build_chat_model()` starts its calls at least 60/N seconds apart, with no
  bursts; a call waits for its turn instead of being sent and refused by the
  provider. The wait counts towards `RUN_TIMEOUT_SECONDS` (B-0006). Each built
  model has its own pacing, as provider quotas are per model. Unset means no
  pacing.

## Acceptance tests
All use a scripted fake model (SPEC-0005).
- AT-0001 (B-0001): Settings for each provider (ollama, anthropic, google, openai)
  build the matching chat model without network calls. Each hosted provider with no
  key is a settings error, and so is an empty key. With `OPENAI_API_BASE` and
  `OPENAI_BASE_URL` set, the OpenAI model's base URL is still OpenAI's.
  -> `tests/test_settings.py::test_provider_selection`,
  `tests/test_settings.py::test_hosted_provider_requires_key`,
  `tests/test_settings.py::test_empty_key_counts_as_missing`
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
- AT-0008 (B-0004): Fake model calls an unknown tool once, then answers → status="ok",
  tool_calls is empty, and the model received a ToolError `invalid_input`.
  -> `tests/test_agent.py::test_invalid_call_returned_to_model`
- AT-0009 (B-0005): Fake model requests web_search, add_note, web_search in one turn →
  the first search runs, then awaiting_confirmation; confirm=true → add_note runs, then
  the second search. -> `tests/test_confirmation.py::test_batch_runs_in_order`
- AT-0010 (B-0007): After a truncated run, the next message's model call receives a
  ToolError `internal` for each call that never ran.
  -> `tests/test_agent.py::test_thread_usable_after_truncation`
- AT-0011 (B-0008): Settings for each provider (ollama, anthropic, google, openai)
  with `LLM_REQUESTS_PER_MINUTE=600` build a model whose calls are spaced 0.1 s
  apart with no burst, and without the setting a model with no pacing; no network
  calls.
  -> `tests/test_settings.py::test_provider_rate_limit`

## Non-goals
Multi-agent routing, streaming, model fallback between providers, a base URL
setting for OpenAI-compatible servers (see Open questions).

## Open questions
All are about the OpenAI provider. Facts about `langchain-openai` were read from
the 1.7.4 release on PyPI on 2026-11-04.
- Dependency: `ChatOpenAI` comes from `langchain-openai`, which is not a dependency
  yet. It needs the user's approval before implementation. Release 1.7.4 needs
  `langchain-core>=1.7.5` and adds `openai`; `uv.lock` has `langchain-core` 1.6.10,
  so approval has to cover that upgrade too, or an older release.
- Rate limits: `rate_limiter` is a LangChain chat model field, so B-0008 applies to
  `ChatOpenAI` unchanged. It paces requests only, so a tokens-per-minute limit can
  still refuse a call (503, SPEC-0002 B-0007). Retries inside the `openai` SDK
  happen within one paced call, so they aren't spaced by B-0008. Proposed: no
  change, and leave `max_retries` at its default.
- Responses API: `ChatOpenAI` picks the Responses API by itself for some model
  names and otherwise uses Chat Completions. Replies then hold content blocks. The
  agent reads `.text`, tool_calls and invalid_tool_calls, so it should cope, but
  saved history (B-0007) is only exercised against a real model in evals. Proposed:
  keep the default, and run `make evals` on the chosen model before SPEC-0001 is
  marked implemented. No OpenAI model is named here; that choice is `LLM_MODEL`.
- Tool schemas: `bind_tools` is not strict by default, which fits the schemas here,
  as they drop `additionalProperties` for Gemini. Arguments are checked against the
  tool's Input model anyway (B-0004). Proposed: stay non-strict.
- Prices: SPEC-0012's price table has no OpenAI rows, and `src/agent/prices.py`
  isn't built yet. With `DAILY_COST_LIMIT_USD` set, an unpriced OpenAI model is a
  settings error (SPEC-0012 B-0011), so the failure is safe. Table rows are added
  with the budgets, for the models actually used; cached input is then charged at
  the full input price, which overstates spend. SPEC-0011 has no prices. Proposed:
  no spec change.

When this is built, `tests/test_settings.py::test_unknown_provider_rejected` needs
another name, as it uses `openai` as the unknown provider, and `OPENAI_API_KEY`
joins `SECRET_ENV_VARS` in `tests/conftest.py` (SPEC-0005). `README.md`, `.env.example`
and `docs/design/design.md` describe what is built, so they change with the code.
