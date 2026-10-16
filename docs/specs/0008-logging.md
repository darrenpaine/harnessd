# SPEC-0008: Logging

Status: implemented
Requirements: none yet
Design: [design.md](../design/design.md)

## Goal
Enough logging to follow a run from the server's output: when it starts, each graph
step, each tool call, and how it ends. Message text and secrets are never logged.

## Inputs / Outputs
### Setting
- `LOG_LEVEL`: `debug` | `info` | `warning` | `error`, case-insensitive. Default
  `info`; `.env.example` sets `LOG_LEVEL=info`. An unknown value fails settings
  validation at startup.

### Output
- Python's standard `logging`, to stderr, no new dependency.
- Loggers are named `harnessd.<module>` (e.g. `harnessd.runner`, `harnessd.graph`).
- One line per event: time (UTC, ISO 8601), level, logger, event name, then
  `key=value` fields. Example:
  `2026-10-16T09:30:00Z INFO harnessd.runner run.start thread=6f1c… kind=message max_tool_calls=5 provider=ollama model=llama3.1`

### Events
| Event | Level | Fields |
|-------|-------|--------|
| `run.start` | info | thread, kind (`message` \| `confirm`), max_tool_calls, provider, model |
| `graph.step` | info | thread, node (`model` \| `tools` \| `confirm`) |
| `model.reply` | info | thread, tool_calls (count requested), ms |
| `tool.call` | info | thread, tool, call_id, result (`ok` or the ToolError code), ms |
| `tool.input` | debug | thread, tool, call_id, input (JSON, cut to 200 chars) |
| `confirm.wait` | info | thread, tool |
| `confirm.decision` | info | thread, tool, approved |
| `run.end` | info | thread, status, tool_calls, ms; plus error code when status is error |
| `thread.busy` | warning | thread |
| `dependency.unavailable` | warning | cause (the 503 message, SPEC-0002 B-0005, B-0007) |
| `request.failed` | error | the exception with its traceback (SPEC-0002 B-0008) |
| `thread.*` | info | the thread endpoint events in SPEC-0009 |

## Behavior
- B-0001: `LOG_LEVEL` sets the level of the `harnessd` loggers. Default `info`.
- B-0002: At `info`, a run logs `run.start`, one `graph.step` per node executed, one
  `model.reply` per model call, one `tool.call` per tool call (including errored
  ones), the confirmation events, and `run.end`, each with the fields above.
- B-0003: Every line logged during a run carries `thread=<thread_id>`.
- B-0004: No level logs the user's message text, the model's answer or reasoning,
  tool outputs, or any API key. Tool inputs are logged only at `debug`, cut to 200
  characters.
- B-0005: 409, 503 and 500 responses are logged at the levels above. A 500 is
  logged once, with its traceback; the response body is unchanged (SPEC-0002 B-0008).
- B-0006: Below `debug`, the `httpx` and `httpcore` loggers are set to `warning`, so
  each provider or tool HTTP request is not logged. Uvicorn's own logging is left
  as is.

## Acceptance tests
All use pytest's `caplog` and a scripted fake model (SPEC-0005).
- AT-0001 (B-0001): `LOG_LEVEL=DEBUG` and `LOG_LEVEL=warning` both parse; `LOG_LEVEL=loud`
  fails validation; unset → `info`. -> `tests/test_logging.py::test_log_level_setting`
- AT-0002 (B-0002, B-0003): A run with one web_search call then an answer logs, in
  order, run.start, graph.step model, model.reply, graph.step tools, tool.call,
  graph.step model, model.reply, run.end status=ok — every line with the thread id.
  -> `tests/test_logging.py::test_run_events_logged`
- AT-0003 (B-0002): An add_note run logs confirm.wait and run.end
  status=awaiting_confirmation; confirming logs confirm.decision approved=true.
  -> `tests/test_logging.py::test_confirmation_events_logged`
- AT-0004 (B-0004): With a message, answer, tool input and API key holding marker
  strings, no log line at `info` contains them; at `debug` only the tool input
  marker appears. -> `tests/test_logging.py::test_no_content_or_secrets_logged`
- AT-0005 (B-0005): A 500 logs request.failed at error level with the traceback; a
  503 logs dependency.unavailable at warning. -> `tests/test_logging.py::test_error_responses_logged`
- AT-0006 (B-0006): At `info`, the httpx logger's effective level is `warning`; at
  `debug` it is `debug`. -> `tests/test_logging.py::test_http_client_logs_quiet`

## Non-goals
Log shipping, metrics, per-module levels. Tracing, JSON output
(`LOG_FORMAT`) and request and trace ids are added by SPEC-0010.

## Open questions
None. The user's message text is never logged, at any level (B-0004).
