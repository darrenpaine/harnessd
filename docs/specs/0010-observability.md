# SPEC-0010: Observability

Status: reviewed
Requirements: none yet
Design: [design.md](../design/design.md)

## Goal
See what the agent did and why, across the logs and a trace UI: a trace per request
showing each model call (prompt, reply, tokens, latency) and tool call, grouped by
conversation, with ids that join a log line to its trace and to the client request.
Everything runs locally on open-source software; tracing is off unless configured.

## Decisions
- **Backend: Langfuse, self-hosted.** It is MIT-licensed at its core and runs
  locally with docker compose. It understands LLM traces natively: generations,
  token counts and costs, prompts and replies. Its sessions match our thread_id.
  It also stores datasets and scores, which the eval spec (SPEC-0011) can reuse.
  The cost is a heavy local stack (Postgres, ClickHouse, Redis, MinIO; plan for
  several GB of RAM), so it runs as an optional compose profile, never in CI.
- **Instrumentation: OpenTelemetry (OTLP export).** OTel is the cross-vendor
  standard for traces. The code emits standard spans with GenAI semantic-convention
  attributes and does not import a Langfuse SDK. Langfuse, Phoenix, Opik and Jaeger
  all accept OTLP, so the backend is a setting, not a code change. This matches the
  provider-agnostic model layer (SPEC-0001 B-0001), and it is the approach most
  teams would recognise.

### Alternatives considered
| Option | Why not chosen |
|--------|----------------|
| Arize Phoenix | Lightest to run (one container, OTel-native) and good agent views, but Elastic License 2.0 is source-available, not OSI open source. Kept as a drop-in OTLP target. |
| Opik (Comet) | Apache 2.0 with tracing, evals and guardrails, but a stack as heavy as Langfuse's and less LangGraph usage. Also a drop-in OTLP target. |
| Pydantic Logfire | Open-source SDK, but the backend is a hosted service; self-hosting is enterprise-only. Fails "local and open source". |
| LangSmith | Hosted, not open source. |
| OTel + Jaeger only | Tiny and vendor-neutral, but no LLM views (tokens, costs, prompts, sessions). |
| Langfuse SDK / LangChain callback | Fewer lines, but ties the code to one vendor; OTLP gives the same traces in Langfuse. |

## Inputs / Outputs
### Settings
| Setting | Default | Meaning |
|---------|---------|---------|
| `TRACING_ENABLED` | `false` | Turn on OTel tracing. Off → no exporter, no spans recorded. |
| `LANGFUSE_HOST` | `http://localhost:3000` | Langfuse base URL; the OTLP endpoint is `<host>/api/public/otel`. |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | none | Langfuse project keys, sent as OTLP basic auth. Required when tracing is on, unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set. |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` | none | Standard OTel variables; when set they override the Langfuse values, to send traces to another backend. |
| `TRACE_CONTENT` | `true` | Record prompts, replies and tool inputs/outputs on spans. `false` keeps only names, ids, timings, token counts and status. Set `false` in shared deployments; `.env.example` says so. |
| `LOG_FORMAT` | `text` | `text` (SPEC-0008 lines) or `json` (one JSON object per line). |

### Span tree (one trace per HTTP request)
```
POST /v1/agent/run                 server span (FastAPI instrumentation)
└─ agent.run                       thread, kind, max_tool_calls, status, tool_calls
   ├─ agent.model                  one per model node; gen_ai.* attributes
   │  └─ chat <model>              the LLM call: gen_ai.request.model, gen_ai.usage.input_tokens / output_tokens
   ├─ agent.tool <name>            call_id, result (ok | ToolError code)
   └─ agent.confirm                tool, approved | awaiting
```
- `session.id` = thread_id on `agent.run`, so Langfuse groups a conversation's
  traces as one session. `user.id` is not set (no auth).
- The thread endpoints (SPEC-0009) get `thread.read`, `thread.delete` and
  `thread.compact` spans; compaction's summarising call is a `chat` span under it.

### Correlation ids
- `request_id`: from the `X-Request-ID` request header if it is 1-128 characters of
  `[A-Za-z0-9._-]`, otherwise a new UUID4. Returned as `X-Request-ID` on every
  response, including 4xx and 5xx.
- `trace_id`: the OTel trace id (32 hex), present only when tracing is on.
- `thread`: as in SPEC-0008.

### JSON log line (`LOG_FORMAT=json`)
```json
{"time":"2026-10-16T09:30:00Z","level":"info","logger":"harnessd.runner","event":"run.start","request_id":"…","trace_id":"…","thread":"…","kind":"message"}
```
Same events and fields as the text format (SPEC-0008); only the encoding differs.

### Local stack
- `docker compose --profile observability up -d` starts Langfuse beside Redis, using
  Langfuse's published images and their documented compose settings.
- `make langfuse` wraps it. The README says how to create a project and copy its
  keys into `.env`.

## Behavior
- B-0001: With `TRACING_ENABLED=false` no exporter is created and no spans are
  recorded; requests behave exactly as without this spec.
- B-0002: With tracing on, each request to `/v1/agent/run` produces one trace with
  the span tree above. Errored tool calls, truncation, timeouts and 409/422/503/500
  responses are recorded as span status and attributes, never dropped.
- B-0003: LLM spans carry the model name and the input and output token counts the
  provider reports. When the provider reports none, the attributes are omitted, not
  set to zero.
- B-0004: With `TRACE_CONTENT=false`, no span holds message text, model replies or
  tool inputs and outputs. API keys never appear on spans in either mode.
- B-0005: Exporting is asynchronous (batch processor). If the backend is down or
  slow, requests are not slowed or failed; export errors are logged at warning,
  at most once a minute.
- B-0006: Every response carries `X-Request-ID` (B-0007 rules). Every log line
  written while handling a request carries `request_id`, and `trace_id` when
  tracing is on. The server span carries `request_id` as an attribute.
- B-0007: A client-supplied `X-Request-ID` that breaks the format rule is replaced,
  not rejected; the request still succeeds.
- B-0008: `LOG_FORMAT=json` writes one JSON object per line with the fields above;
  `text` keeps the SPEC-0008 format with `request=` and `trace=` fields added.
  An unknown value fails settings validation.
- B-0009: Tracing settings are validated at startup: tracing on with neither
  Langfuse keys nor an OTLP endpoint is a settings error.

## Acceptance tests
All use an in-memory span exporter and a scripted fake model; none need Langfuse.
- AT-0001 (B-0001): Tracing off → a run records no spans.
  -> `tests/test_tracing.py::test_tracing_off_records_nothing`
- AT-0002 (B-0002, B-0003): A run with one tool call → spans server → agent.run →
  agent.model → chat, agent.tool, agent.model → chat; token counts from the fake
  model's usage metadata are on the chat spans; session.id is the thread id.
  -> `tests/test_tracing.py::test_run_span_tree`
- AT-0003 (B-0002): A run that times out, and a 503 → spans have error status and
  the error code. -> `tests/test_tracing.py::test_failures_recorded`
- AT-0004 (B-0004): With marker strings in the message, reply, tool input and an API
  key: TRACE_CONTENT=true → message marker on spans, key absent; false → no marker.
  -> `tests/test_tracing.py::test_trace_content_switch`
- AT-0005 (B-0005): An exporter that raises or hangs → the run still returns 200 in
  normal time. -> `tests/test_tracing.py::test_exporter_failure_isolated`
- AT-0006 (B-0006, B-0007): A valid X-Request-ID is echoed; an invalid or missing
  one is replaced with a UUID; 422 and 500 responses carry it; log lines carry it.
  -> `tests/test_correlation.py::test_request_id`
- AT-0007 (B-0006): With tracing on, a run's log lines carry the trace id of its
  spans. -> `tests/test_correlation.py::test_logs_carry_trace_id`
- AT-0008 (B-0008): LOG_FORMAT=json → each line parses as JSON with event and
  fields; text → SPEC-0008 format plus request=. Unknown value → settings error.
  -> `tests/test_logging.py::test_log_format`
- AT-0009 (B-0009): TRACING_ENABLED=true without keys or endpoint → settings error.
  -> `tests/test_settings.py::test_tracing_settings_validated`

## Non-goals
Metrics and dashboards (Prometheus/Grafana), log shipping, sampling policies,
alerting, user identity on traces, Langfuse prompt management.

## Open questions
None. Settled in review:
- LLM `chat` spans come from `openinference-instrumentation-langchain` (Apache 2.0),
  which hooks LangChain's callbacks so every model call is captured without code in
  the graph, and whose attributes Langfuse maps. Dependencies to add at
  implementation: `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`,
  `opentelemetry-instrumentation-fastapi`, `openinference-instrumentation-langchain`.
- `TRACE_CONTENT` defaults to `true`: seeing the exact prompt and reply is the main
  reason to trace, and the stack is local. `.env.example` and the README say to set
  it to `false` for shared deployments (SPEC-0013).
