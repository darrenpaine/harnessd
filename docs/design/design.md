# Design: harnessd agent service

Status: reviewed
Specs: SPEC-0001 Agent core, SPEC-0002 API service, SPEC-0003 Tool contract,
SPEC-0004 State and persistence, SPEC-0005 Test strategy, SPEC-0006 Traceability,
SPEC-0007 API documentation, SPEC-0008 Logging, SPEC-0009 Thread management,
SPEC-0010 Observability (see [the spec index](../specs/README.md))

This document pulls together how the service is built and why. The specs are the
contract; where this document and a spec disagree, the spec wins and this document
is out of date.

## Problem and constraints
Create an example Claude Code workspace and demonstrate with a FastAPI service that uses an
LLM and tools.

The FastAPI service exposes one LangGraph agent. The agent answers a user message,
calling up to three tools, and every response is a validated, structured
`AgentResponse`. Conversation state lives in Redis so a thread can be continued, or
resumed after a human confirms a side effect.

Constraints:
- **Provider-agnostic model**: Ollama locally, Anthropic Claude or Google Gemini
  hosted, chosen by settings with no code change (SPEC-0001 B-0001).
- **Typed boundaries**: Pydantic v2 models for the API and for every tool's input
  and output; unknown fields are rejected.
- **Bounded runs**: a tool-call cap per request, a time limit per run and per tool,
  and one run per thread at a time.
- **Deterministic CI**: no real LLM or web calls and no API keys; real Redis only
  where persistence is under test (SPEC-0005).
- **Spec-first and traceable**: behavior is specified before it is coded, and tests
  name the spec behaviors they prove (SPEC-0006).
- **Non-goals**: auth, streaming, multi-agent routing, a UI, production deployment.

## Proposed approach
Starting with a basic version, develop and update the CLAUDE.md and other associated
files as the project progresses. Add on to these as new rules are needed.

FastAPI handles HTTP and validation. Each request goes to an agent runner that takes
a per-thread lock in Redis, runs the LangGraph graph against a Redis checkpointer
under a time limit, then refreshes the thread's TTL. The graph alternates between
the chat model and the tools until the model answers, the tool-call cap is reached,
or a side-effecting tool needs confirmation. Tools never raise: every failure comes
back to the model as a `ToolError`.

```mermaid
flowchart LR
    client([Client]) -->|POST /v1/agent/run| api[FastAPI app<br/>src/app.py, src/api/]
    client -->|/docs, /redoc| api
    api -->|AgentRequest| runner[Agent runner<br/>src/agent/runner.py]
    runner -->|thread lock, TTL refresh| redis[(Redis 8.2)]
    runner --> graph_node[LangGraph agent<br/>src/agent/graph.py]
    graph_node <-->|checkpoints| redis
    graph_node --> llm[Chat model<br/>src/agent/llm.py]
    llm -.-> providers[Ollama / Claude / Gemini]
    graph_node -->|call_tool| tools[Tools<br/>src/tools/]
    tools -.-> tavily[Tavily API]
    tools -.-> web[Public web pages]
    tools -.-> notes[(notes.txt)]
    settings[Settings<br/>src/settings.py] -.-> api & runner & llm & tools
```

### Components
Status: **built** = on main; **review** = in an open PR; **planned** = specified, not
yet written.

| Module | Responsibility | Spec | Status |
|--------|----------------|------|--------|
| `src/app.py` | Builds the FastAPI app: Swagger/ReDoc metadata, lifespan (logging, tracing, `Services`), error handlers, the request id middleware, the routers and the FastAPI instrumentation (outermost, so error responses are traced). | SPEC-0002, SPEC-0007, SPEC-0010 | built |
| `src/logs.py` | `configure_logging()` (level, text or JSON lines); `event()` writes one event line with the fields of any enclosing `log_context()` block (request id, thread) and the current trace id. | SPEC-0008, SPEC-0010 B-0006, B-0008 | built |
| `src/tracing.py` | `configure_tracing()`: OTLP export in batches to Langfuse (or `OTEL_EXPORTER_OTLP_ENDPOINT`), OpenInference instrumentation of LangChain, failed exports logged at most once a minute. `PROVIDER` hands spans to whichever provider is configured (no-op when off), since OTel's global provider and FastAPI's middleware can each be set only once. Our spans: `agent.run`, `agent.tool <name>`, `agent.confirm`, `thread.*`. | SPEC-0010 | built |
| `src/api/request_id.py` | Pure ASGI middleware: takes or makes the request id, returns it as `X-Request-ID`, tags log lines and the server span with it. | SPEC-0010 B-0006, B-0007 | built |
| `src/api/agent.py`, `src/api/threads.py`, `src/api/health.py` | One `APIRouter` per area: `POST /v1/agent/run` (turns `ConfirmationStateError` into 422); `GET`/`DELETE /v1/threads/{id}` and `POST /v1/threads/{id}/compact`; `GET /health`. | SPEC-0002, SPEC-0009 | built |
| `src/api/deps.py` | `Services` builds the Redis client, chat model and graph on first use, so the app starts while Redis is down. Dependencies `get_settings` → `get_graph` / `get_guard` / `get_model` → `get_runner` / `get_threads`; tests override the graph, guard and model. | SPEC-0002, SPEC-0009 | built |
| `src/api/errors.py` | Exception handlers: `ThreadBusyError`, `ThreadAwaitingConfirmationError` and `ThreadRunUnfinishedError` → 409, `ThreadNotFoundError` → 404, `SummaryError` → 500, Redis/LLM connection errors and provider refusals → 503, anything else → 500 via `InternalErrorMiddleware`. `error_responses()` documents a route's error codes. | SPEC-0002, SPEC-0007 B-0004, SPEC-0008 B-0005 | built |
| `src/schemas.py` | Pydantic models: AgentRequest/AgentResponse, ErrorInfo, PendingAction, ToolError, each tool's Input/Output, the tool-call trace, ThreadState and the compaction request/response. | SPEC-0002, SPEC-0003, SPEC-0009 | built |
| `src/settings.py` | One `Settings` class from env and `.env`; fails at startup if a hosted provider has no key. `get_settings()` is cached for injection. | SPEC-0001, SPEC-0004 | built |
| `src/agent/llm.py` | `build_chat_model()`: ChatOllama, ChatAnthropic or ChatGoogleGenerativeAI from settings, no network calls. | SPEC-0001 B-0001 | built |
| `src/agent/persistence.py` | `open_checkpointer()` (shallow Redis checkpointer with TTL; `ShallowRedisSaver` loads only the latest checkpoint's pending writes, SPEC-0004 B-0006), `refresh_thread_ttl()`, `thread_lock()` / `ThreadBusyError`, the `ThreadGuard` protocol (lock, TTL reset, time to expiry) the runner and thread service depend on, and `RedisThreadGuard`, which implements it. | SPEC-0004, SPEC-0009 B-0001 | built |
| `src/tools/base.py` | Tool contract: `Tool` metadata, `ToolContext` (settings, HTTP transport, DNS resolver), `ToolFailure`, `call_tool()`. | SPEC-0003 B-0001..B-0003 | built |
| `src/tools/{web_search,fetch_site,add_note}.py` | The three tools; registry in `src/tools/__init__.py`. | SPEC-0003 B-0005..B-0007 | built |
| `src/agent/graph.py` | The agent loop: `model` node (cap and invalid-call checks), `tools` node (runs a turn's calls in order, stops before a side effect), `confirm` node (interrupt, then run or reject). `build_prompt` answers dangling calls and puts a compacted thread's summary in the system prompt. A run's input (`RunInput`) never carries the summary, so it survives later runs. | SPEC-0001 B-0002..B-0005, B-0007, SPEC-0009 B-0005 | built |
| `src/prompts/` | One file per prompt (`system.md`, `summary.md`); `prompts.load()` reads them at import, so a missing file fails at startup. The graph and the thread service import `SYSTEM_PROMPT` and `SUMMARY_PROMPT` from here. | SPEC-0001, SPEC-0009 B-0005 | review |
| `src/agent/runner.py` | What the API calls: `ThreadGuard.hold` (lock) → confirmation rules → graph run under `RUN_TIMEOUT_SECONDS` → `ThreadGuard.touch` (TTL) → `AgentResponse`. Raises `ThreadBusyError` and `ConfirmationStateError`; dependency errors propagate for the API to map to 503. | SPEC-0001 B-0006, SPEC-0002 B-0003, SPEC-0004 | built |
| `src/agent/threads.py` | `ThreadService`: read a thread's state (no lock, no TTL reset); delete it (lock → `adelete_thread`); compact it (lock → summary call under `RUN_TIMEOUT_SECONDS` → `RemoveMessage` for whole turns + `summary` via `aupdate_state` → TTL reset). | SPEC-0009 | built |
| `scripts/export_openapi.py` | Writes `docs/openapi.json` (`make openapi`). | SPEC-0007 B-0006 | built |
| `scripts/trace.py` | Builds `docs/traceability.md` from specs and test markers and flags gaps (`make trace`; run in CI). | SPEC-0006 | built |
| `tests/fakes.py` | `ScriptedChatModel`: replays scripted replies, records inputs. | SPEC-0005 B-0002 | built |
| `tests/conftest.py`, `tests/redis_support.py` | Clears API keys for every test; `redis_url` fixture (skip locally, fail in CI); `tool_context` fixture. | SPEC-0005 | built |
| `Dockerfile`, `docker-compose.yml` | App image; Redis 8.2 with data in a host folder (`REDIS_DATA_DIR`); Langfuse and its stores under the `observability` profile (`make langfuse`). | SPEC-0010 | built |
| `.github/workflows/ci.yaml` | Lint, format check, mypy; pytest with a Redis 8.2 service container. | SPEC-0005 | built |

### Data flow
A new message, then a confirmation of a side-effecting tool:

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as FastAPI
    participant R as Runner
    participant D as Redis
    participant G as Graph
    participant M as Chat model
    participant T as Tools

    C->>A: POST /v1/agent/run {message}
    A->>A: validate AgentRequest (else 422, no LLM call)
    A->>R: run(request)
    R->>D: take lock harnessd:lock:thread:{id} (else 409 thread_busy)
    R->>G: invoke under RUN_TIMEOUT_SECONDS
    G->>D: load latest checkpoint (unknown/expired id → new thread)
    loop until answer, cap, or side effect
        G->>M: messages + tool schemas
        M-->>G: answer or tool calls
        G->>T: call_tool (read-only tools only)
        T-->>G: Output or ToolError
        G->>D: checkpoint after each step
    end
    Note over G: side-effecting tool requested → interrupt
    G-->>R: run result
    R->>D: refresh TTL on every key of the thread
    R->>D: release lock
    R-->>A: AgentResponse (status ok / truncated / error / awaiting_confirmation)
    A-->>C: 200 AgentResponse

    C->>A: POST /v1/agent/run {thread_id, confirm: true}
    A->>R: run(request)
    R->>D: take lock
    R->>G: Command(resume=true)
    G->>T: call_tool(add_note) once
    G->>M: continue with the tool result
    G-->>R: run result
    R->>D: refresh TTL, release lock
    A-->>C: 200 AgentResponse
```

With `confirm: false`, the graph records ToolError `rejected` for the pending call and
continues. Sending a `message` while a confirmation is pending, or `confirm` when
nothing is pending, is a 422 (SPEC-0002 B-0003).

### Agent graph
The graph has three nodes (`src/agent/graph.py`, SPEC-0001). Each node returns a
`Command` naming the next node, and the checkpointer saves the state after every
node, so a run can stop at any point and resume later.

```mermaid
flowchart TD
    start([START]) --> model
    model["model<br/>send build_prompt(state) to the chat model"]
    model -->|answer| ok([END: ok])
    model -->|no answer, or invalid<br/>calls twice in a row| error([END: error<br/>model_output_invalid])
    model -->|calls exceed max_tool_calls| truncated([END: truncated])
    model -->|calls queued| tools
    tools["tools<br/>run queued calls in order;<br/>invalid calls get ToolError invalid_input"]
    tools -->|queue empty| model
    tools -->|next call has a side effect| confirm
    confirm["confirm<br/>interrupt with PendingAction"]
    confirm -.->|"pause: awaiting_confirmation"| client([Client])
    client -.->|"confirm true / false: Command(resume=...)"| confirm
    confirm -->|"ran, or ToolError rejected;<br/>more calls queued"| tools
    confirm -->|queue empty| model
```

The state carries the thread's `messages`, the `queue` of calls from the latest model
turn that have not run yet, the run counters (`max_tool_calls`, `calls_used`,
`trace`, `outcome`, reset on each request) and `invalid_turns`. A paused `confirm`
keeps its `queue` in the checkpoint, so the calls after it run once the user
answers. The runner turns the final state into an `AgentResponse`.

### Data models and storage
**API** (`src/schemas.py`, SPEC-0002):
- `AgentRequest`: thread_id, exactly one of `message` / `confirm`, max_tool_calls (1..20).
- `AgentResponse`: a model validator keeps the fields consistent with `status`.
- `ErrorInfo`; `PendingAction`; `ApiError` for 404/409/500/503 (SPEC-0007); `HealthResponse`.
- `ThreadState` (messages as user/assistant/tool `ThreadMessage`s, no system prompt or
  run counters), `CompactRequest`, `CompactResponse` (SPEC-0009).

**Tools** (SPEC-0003):
- `ToolError` with codes invalid_input, timeout, upstream_error, rejected, internal.
- Each tool's Input/Output model.
- The trace is a list of `ToolCallRecord`s, a union discriminated by tool name, with
  exactly one of output / error set.

**Redis** (SPEC-0004):

| Key | Written by | Expiry |
|-----|-----------|--------|
| `checkpoint:<thread>:…`, `checkpoint_write:<thread>:…` and related | `ShallowRedisSaver`, our subclass of `AsyncShallowRedisSaver` (latest state per thread only) | `CHECKPOINT_TTL_HOURS` (24 h), reset on every key after each run |
| `harnessd:lock:thread:<thread>` | `thread_lock()` | `RUN_TIMEOUT_SECONDS` + 30 s |

The checkpointer sets a TTL only on keys it writes in the current run, so keys from
earlier runs would expire first. `refresh_thread_ttl()` therefore re-applies the TTL
to every key containing the thread id (a UUID) after each run, except the lock key.
`GET /v1/threads/{id}` reports the smallest of those TTLs as `expires_in_seconds`.

**Files**:
- Notes: `NOTES_PATH` (default `data/notes.txt`), one
  `YYYY-MM-DDTHH:MM:SSZ<TAB>text` line per note.
- Notes under docker compose: `NOTES_DIR` (default `data/notes`), mounted at
  `/srv/data/notes` in the app container.
- Local Redis data: `REDIS_DATA_DIR` (default `data/redis`).
- All of these live under the gitignored `data/` by default.

### Failure modes
| What goes wrong | Response | Spec |
|-----------------|----------|------|
| Invalid request body | 422, model never called | SPEC-0002 B-0001 |
| `message` while awaiting confirmation, or `confirm` with nothing pending | 422 | SPEC-0002 B-0003 |
| Another run holds the thread | 409 `thread_busy`, state untouched | SPEC-0002 B-0004, SPEC-0004 B-0005 |
| Redis or LLM provider unreachable | 503 `dependency_unavailable` | SPEC-0002 B-0005 |
| Tool fails (bad args, upstream error, exception) | ToolError returned to the model; run continues | SPEC-0003 B-0001, SPEC-0001 B-0003 |
| Tool exceeds its timeout | ToolError `timeout` | SPEC-0003 B-0002 |
| Tool-call cap reached | status `truncated` | SPEC-0001 B-0002 |
| Model output unusable (bad tool args twice, no answer) | status `error`, `model_output_invalid` | SPEC-0001 B-0004 |
| Run exceeds `RUN_TIMEOUT_SECONDS` | status `error`, `internal` | SPEC-0001 B-0006 |
| Process dies mid-run | lock expires after timeout + 30 s; thread usable again | SPEC-0004 B-0005 |
| fetch_site URL (or redirect) points at a non-public address | ToolError `invalid_input` | SPEC-0003 B-0006 |
| Thread id unknown or expired | new thread with that id; 404 `thread_not_found` on the thread endpoints | SPEC-0004 B-0002, SPEC-0009 B-0001 |
| Compacting a thread awaiting confirmation | 409 `thread_awaiting_confirmation` | SPEC-0009 B-0007 |
| Compacting a thread whose last run timed out mid-step | 409 `thread_run_unfinished`; the next message finishes it | SPEC-0009 B-0011 |
| Summary call times out or returns no text | 500 `internal`, `thread.compact_failed` warning, state unchanged (a provider error is a 503) | SPEC-0009 B-0008 |
| Trace backend down or slow | Requests unaffected (export runs on the batch processor's thread); `trace.export_failed` warning at most once a minute | SPEC-0010 B-0005 |
| Redis lacks the JSON/search modules | `redis` tests skip locally / fail in CI with a clear reason | SPEC-0005 B-0003 |

## Alternatives considered
| Option | Pros | Cons | Why not |
|--------|------|------|---------|
| Another agent framework: a hand-written tool loop on the provider SDKs, PydanticAI, a single-provider SDK (OpenAI Agents SDK, Anthropic's tool runner) or LlamaIndex | Fewer dependencies (hand-written); typed agents (PydanticAI); first-party tooling (provider SDKs) | Checkpointing, pause-and-resume and history repair to build ourselves, or no ready-made Redis checkpointer; provider SDKs centre on one vendor's API | LangGraph gives a Redis checkpointer (`langgraph-checkpoint-redis`), `interrupt()` / `Command(resume=…)` for confirmations (SPEC-0001 B-0005), provider-neutral chat models (SPEC-0001 B-0001), and explicit nodes that run on a scripted model in tests |
| Redis Stack image instead of Redis 8.2 | Long-standing way to get JSON + search | Older line; was pinned to `:latest` | Redis 8+ bundles both modules in the official image |
| Full checkpointer (`AsyncRedisSaver`) | Keeps history for time travel | More keys per thread; history unused | We only ever resume the latest state |
| `refresh_on_read` TTL option | No extra code | Measured: earlier runs' keys keep their old countdown | Doesn't meet SPEC-0004 B-0003 |
| Global B/AT ids across specs | Fewer test edits when splitting | Gaps in every spec's numbering | Per-spec ids, always written with the spec id |
| Mock the checkpointer in tests | No Redis needed | Misses real TTL, lock and index behavior | Real Redis via fixture; graph tests use `InMemorySaver` |
| LangChain `init_chat_model` | One call for any provider | Needs the `langchain` package | A small factory over the three provider packages |
| Hand-written Tavily fake | Simple | Skips the real client's request/response handling | Real client on an `httpx.MockTransport` |
| `DOCS_ENABLED` setting | Can hide docs | Extra config for a non-production service | Docs always on |
| Set OTel's global tracer provider at startup | The usual OTel setup | Can be set once per process, and FastAPI's middleware fetches its tracer once, so tests couldn't switch tracing on and off | A switching provider that delegates each span to the configured one |
| Build the API before tools and graph | Swagger to try early | Needs a placeholder runner | Tools → graph → API, each piece real when it lands |

## Decision
- One LangGraph agent behind a runner that owns the lock, timeout and TTL refresh, so
  the API layer stays thin and testable with a fake runner.
- Redis 8.2 with the shallow checkpointer and an explicit TTL refresh; a Redis lock
  per thread. Trade-off: a `SCAN` per run and a dependency on Redis modules.
- Tools follow one contract and never raise; side effects need confirmation through
  a LangGraph interrupt. Trade-off: the confirmation is a second HTTP request.
- Tests use a scripted model and mocked HTTP/DNS; only persistence tests use real
  Redis. Trade-off: provider-specific quirks aren't exercised in CI.

## Risks and open questions
- **DNS rebinding** (fetch_site): addresses are checked before the request, but
  `httpx` resolves again when connecting. Fix by connecting to the checked IP if the
  service is ever exposed.
- **TTL refresh cost**: `SCAN` over the keyspace per run is fine at this scale but
  grows with all of Redis. Planned fix: renew only the thread's keys through the
  checkpointer's own per-thread index ([#14](https://github.com/darrenpaine/harnessd/issues/14)).
- **add_note after timeout**: the file write runs in a worker thread that a timeout
  can't stop, so a note may be written even though the tool reported `timeout`.
- **Swagger assets** load from a CDN, so `/docs` needs internet access in the browser.
- **Detecting an unreachable dependency** relies on exception types: Redis
  `ConnectionError`/`TimeoutError` (also when wrapped by redisvl's
  `RedisSearchError`), `httpx.TransportError` (Ollama, Gemini) and LangChain's
  `ModelConnectionError` (Anthropic). A provider that raises something else gives a
  500 instead of a 503.
- **Span parents in graph nodes**: OpenInference doesn't make its node spans current
  in the OTel context, so `agent.tool` and `agent.confirm` find their parent with
  its `get_current_span()` (from LangChain's run config). A change there would
  attach them to `agent.run` instead; `test_run_span_tree` would catch it.
- **Langfuse stack** needs several GB of RAM (Postgres, ClickHouse, Redis, MinIO),
  so it runs only under the `observability` profile, never in CI.
- **langgraph-checkpoint-redis private API**: version 0.5.2 loads every pending
  write a thread ever made, not just the latest checkpoint's, so a second
  confirmation on a thread failed with `InvalidUpdateError`. `ShallowRedisSaver`
  overrides the private `_aload_pending_writes` and relies on the library's key
  layout and write documents, so an upgrade can break it silently. Import fails
  if the method disappears, and SPEC-0004 AT-0006
  (`test_repeated_confirmations`) fails if it stops applying; re-check the class on
  every upgrade, and drop it once the library is fixed
  ([langgraph-redis#215](https://github.com/redis-developer/langgraph-redis/issues/215)). The old writes still stay
  in Redis until the thread expires.
- **langgraph-checkpoint-redis** emits a deprecation warning about
  `get_async_redis_connection`; watch for a breaking release.
- **Traceability script** (SPEC-0006) isn't built yet, so gaps are checked by hand.
  SPEC-0005 AT-0005 waits on it.
- **Local Ollama**: the default model (`llama3.1`) must be pulled before local runs.
