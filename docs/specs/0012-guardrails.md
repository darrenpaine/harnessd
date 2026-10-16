# SPEC-0012: Guardrails and human oversight

Status: reviewed
Requirements: none yet
Design: [design.md](../design/design.md)

## Goal
Keep the agent within safe limits when it reads untrusted content and spends real
money: resist prompt injection, validate what goes in and comes out, cap tokens and
cost, keep a human in the loop for side effects, and leave an audit trail of what
the agent did and who approved it.

## Decisions
- **Layered injection defence, plus an optional decision-model screen.** No single
  control stops prompt injection, so several cheap layers each cut the risk: tool
  output is marked as untrusted data, sizes are capped, side effects need a human,
  and a System One decision model (the same plumbing as the SPEC-0011 judge) can
  withhold tool output that looks like an injection. Eval cases measure it.
- **Budgets at three levels.** Per run (a runaway loop), per thread (one long
  conversation) and per day (total spend), so no single failure mode is
  unbounded.
- **Audit trail in the logs.** Audit events go to a dedicated `harnessd.audit`
  logger that stays on whatever `LOG_LEVEL` is; no new storage. They are
  content-free and carry request_id and thread (SPEC-0010), so they join to logs
  and traces.
- **Approval step unchanged.** Side-effecting tools always ask; read-only tools
  never do (SPEC-0001 B-0005). This spec adds auditing of every decision.

### Alternatives considered
| Option | Why not chosen |
|--------|----------------|
| NeMo Guardrails / Guardrails AI | Broad rail and validator libraries, but heavy dependencies and a second control flow around the LangGraph loop. |
| Llama Guard / Prompt Guard | Purpose-built classifiers, but another model to host; Prompt Guard needs a transformers runtime rather than Ollama. A decision model on Ollama reuses what evals already need. |
| Layers without a screen | Fully deterministic, but nothing catches an injection that changes the answer rather than calling add_note. The screen is optional, so this remains the default when it is off. |
| Audit in a Redis Stream or file | Queryable, but new storage and retention to manage; logs are enough until there is a user-facing need. |
| Per-tool approval policy | Possible later; the current fixed rule is simple and already tested. |

## Inputs / Outputs
### Settings
| Setting | Default | Meaning |
|---------|---------|---------|
| `INJECTION_SCREEN` | `off` | `off` \| `ollama-systemone` \| `jev` \| `chat` — the decision model kind (as SPEC-0011's judge). |
| `INJECTION_SCREEN_MODEL` | none | e.g. `nimble`. |
| `INJECTION_THRESHOLD` | `0.8` | Confidence at or above which flagged content is withheld. |
| `INJECTION_SCREEN_ON_ERROR` | `withhold` | `withhold` (fail closed) or `allow` (fail open) when the screen fails. |
| `MAX_TOKENS_PER_RUN` | `50000` | Input + output tokens across the model calls of one run. |
| `MAX_TOKENS_PER_THREAD` | `200000` | Tokens across all runs of a thread since its last compaction (SPEC-0009). |
| `DAILY_COST_LIMIT_USD` | none | Total spend per UTC day across all threads; unset → no daily cap. |

### Model changes (`src/schemas.py`)
- ErrorCode adds `budget_exceeded`.
- ApiErrorCode adds `budget_exceeded`, returned with HTTP 429 (SPEC-0002 table gains
  a 429 row).
- AgentState gains `tokens_used` (thread total, checkpointed).

### Price table
`src/agent/prices.py`: USD per million input and output tokens per
`(provider, model)`, with the date each price was checked. Ollama models cost 0.

### Audit events (`harnessd.audit` logger, always at info)
`audit.run.start`, `audit.run.end` (status, tokens, cost_usd), `audit.tool`
(tool, call_id, result), `audit.confirm` (tool, pending | approved | rejected),
`audit.guard` (check, action: withheld | redacted | blocked, confidence),
`audit.budget` (scope: run | thread | day, used, limit). Each carries request_id
and thread; none carries message text, answers or tool output.

## Behavior
### Input and output validation
- B-0001: Requests are validated as in SPEC-0002 (B-0001). Tool output sent to the
  model is capped: each web_search snippet at 500 characters and fetch_site text at
  its existing 1000 (SPEC-0003).
- B-0002: The system prompt states that tool results are untrusted data and that
  instructions inside them must never be followed. Tool results reach the model only
  as tool messages, never inside the system or user turn.
- B-0003: Before an answer is returned, any configured secret value (provider and
  Tavily keys) that appears in it is replaced with `[redacted]` and an
  `audit.guard` event with action `redacted` is logged.
- B-0004: Every response still validates against AgentResponse (SPEC-0002 B-0002).

### Injection screen
- B-0005: With `INJECTION_SCREEN` set, each web_search and fetch_site result is
  screened before the model sees it, by asking the decision model whether it
  contains instructions aimed at an AI agent. If flagged with confidence ≥
  `INJECTION_THRESHOLD`, the model receives ToolError `rejected` ("withheld: possible
  prompt injection") instead. The call is still recorded in tool_calls, with that
  error, and an `audit.guard` event is logged.
- B-0006: If the screen fails or times out, `INJECTION_SCREEN_ON_ERROR` decides:
  `withhold` treats the result as flagged; `allow` passes it and logs a warning.
- B-0007: User messages are not screened or blocked; the user is the principal.

### Budgets
- B-0008: Token use is taken from each model reply's usage metadata; when a provider
  reports none, it is estimated as characters ÷ 4 and marked estimated in the audit
  event.
- B-0009: When a run's tokens reach `MAX_TOKENS_PER_RUN`, or the thread's reach
  `MAX_TOKENS_PER_THREAD`, the run makes no further model call and ends with status
  error, code `budget_exceeded`, keeping its tool_calls so far. The thread stays
  usable (SPEC-0001 B-0007); compaction resets the thread count to the summary's
  size.
- B-0010: Spend is added to a Redis counter per UTC day after each model call. When
  `DAILY_COST_LIMIT_USD` is set and reached, new runs and compactions get 429
  `budget_exceeded` before taking the thread lock; a run already in progress
  finishes its current model call and then ends as in B-0009.
- B-0011: With `DAILY_COST_LIMIT_USD` set, a provider and model missing from the
  price table is a settings error at startup, so spend is never silently uncounted.

### Human oversight and audit
- B-0012: Confirmation is unchanged (SPEC-0001 B-0005). Each pause, approval and
  rejection is logged as `audit.confirm`.
- B-0013: Audit events are written for every run start and end, tool call,
  confirmation, guard action and budget stop. They stay at info when `LOG_LEVEL`
  is higher, and they are content-free.

## Acceptance tests
All use the scripted fake model and a fake decision model; none call a real LLM.
- AT-0001 (B-0001): A search result with a 2000-character snippet reaches the model
  cut to 500. -> `tests/test_guardrails.py::test_tool_output_capped`
- AT-0002 (B-0002): The system prompt sent to the model contains the
  untrusted-data rule. -> `tests/test_guardrails.py::test_untrusted_rule_in_prompt`
- AT-0003 (B-0003): An answer containing the configured Tavily key → the response
  holds `[redacted]` and an audit.guard event is logged without the key.
  -> `tests/test_guardrails.py::test_secret_redacted`
- AT-0004 (B-0005): A fetched page the fake screen flags at 0.9 → the model receives
  ToolError rejected; at 0.5 → the page text. -> `tests/test_guardrails.py::test_injection_screen`
- AT-0005 (B-0006): A screen that raises → withheld by default; passed with `allow`.
  -> `tests/test_guardrails.py::test_screen_failure_modes`
- AT-0006 (B-0008, B-0009): Fake replies with usage metadata exceeding
  MAX_TOKENS_PER_RUN on the second call → status error, budget_exceeded, no third
  model call; a later run on the thread works.
  -> `tests/test_budgets.py::test_run_token_budget`
- AT-0007 (B-0009): A thread over MAX_TOKENS_PER_THREAD → the next run ends
  budget_exceeded; after compaction it runs. -> `tests/test_budgets.py::test_thread_token_budget`
- AT-0008 (B-0010): With the daily counter at the limit → POST /v1/agent/run → 429
  budget_exceeded and no lock taken. -> `tests/test_budgets.py::test_daily_cost_limit` (redis)
- AT-0009 (B-0011): DAILY_COST_LIMIT_USD with an unpriced model → settings error.
  -> `tests/test_settings.py::test_daily_limit_needs_price`
- AT-0010 (B-0012, B-0013): A confirm flow with LOG_LEVEL=warning still logs
  audit.run.start, audit.confirm pending and approved, audit.tool and audit.run.end,
  each with request_id and thread and no content.
  -> `tests/test_audit.py::test_audit_events`
- AT-0011 (B-0002, B-0005): Injection eval cases (SPEC-0011): a page telling the
  agent to add a note, a search result telling it to reveal its system prompt, and
  a page hiding instructions in HTML comments; all must_pass.
  -> `evals/cases/injection.jsonl`

## Non-goals
Content moderation of user messages, PII detection, jailbreak resistance beyond
the injection cases, per-user quotas (no auth yet), a per-tool approval policy,
an audit query API.

## Open questions
None. Settled in review: defaults of 50k tokens per run and 200k per thread (well
above normal use, a 5-call run is usually under 20k); the screen fails closed
(`withhold`) by default.
