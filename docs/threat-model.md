# Threat model

What could go wrong with harnessd from a security point of view, what stops it, and
what is left open. It uses two passes: STRIDE over each trust boundary, then the
OWASP Top 10 for LLM Applications (2025), which covers the agent-specific threats
STRIDE has no natural place for.

This file links and does not restate: a mitigation lives in the spec named beside
it, and the tests for that behavior are in [traceability.md](traceability.md).
Update it when a spec adds or changes a boundary, a tool, a secret or a limit.

Status values:

| Status | Meaning |
|--------|---------|
| mitigated | A specified, tested behavior stops it. |
| partly | Something reduces it; the gap is described. |
| planned | A reviewed spec covers it, not built yet. |
| accepted | Known and left as is, with the reason. |
| open | No decision yet. |

## Scope and assumptions

- harnessd is an example service, run locally with docker compose. Production
  deployment is a non-goal for now; SPEC-0013 (reviewed, not built) describes one.
- **There is no authentication.** Anyone who can reach port 5999 can use every
  endpoint. The model below assumes the API is reachable only by its operator, and
  marks what changes if that stops being true.
- The user is the principal: their messages are not screened (SPEC-0012 B-0007).
  The attacker is someone else: the author of a web page or search result, another
  party on the network, or a client who should not have access.
- The model is an unreliable component. Anything it returns is validated, and
  anything it may do is bounded by code, never by the prompt alone.

### Assets

| Asset | Where it lives |
|-------|----------------|
| Secrets: provider keys, Tavily key, Langfuse keys, OTLP headers | Environment / `.env`, process memory |
| Thread contents: messages, summaries, tool results | Redis checkpoints (24 h TTL), traces when `TRACE_CONTENT=true` |
| The notes file | `NOTES_PATH` on the host volume |
| Money: provider spend | The provider account; the day's spend counter in Redis |
| The internal network | Reachable from the app container |
| The audit record | `harnessd.audit` log lines |

### Trust boundaries

| # | Boundary | Crossing it |
|---|----------|-------------|
| TB1 | Client → API | HTTP requests on `/v1/*`, `/health`, `/docs` |
| TB2 | API → Redis | Checkpoints, thread locks, the spend counter |
| TB3 | Agent → model provider | Prompts and replies (Ollama, Anthropic, Google, OpenAI) |
| TB4 | Tools → internet | web_search (Tavily), fetch_site (any public URL) |
| TB5 | Tool results → model | Untrusted text entering the model's context |
| TB6 | Agent → notes file | add_note's write |
| TB7 | App → logs and traces | stdout, OTLP export to Langfuse |
| TB8 | Build and supply chain | Dependencies, container image, CI |

## STRIDE by boundary

### TB1: Client → API

| Id | STRIDE | Threat | Mitigation | Status |
|----|--------|--------|------------|--------|
| T-01 | Spoofing | Any caller can use the API as if they were the operator. | No authentication (a non-goal). Compose publishes the API on 127.0.0.1 only, so callers must be on the same machine. SPEC-0013 B-0001 adds an `X-API-Key` check. | planned; accepted locally (non-goal) |
| T-02 | Info disclosure, Tampering | A caller who knows a thread id can read, continue, compact or delete that thread. Ids are UUIDs, but a client may choose its own, and they appear in logs and traces. | Server-made ids are uuid4. No per-thread owner. | accepted (single operator); revisit with auth |
| T-03 | Elevation | A caller approves a pending action they did not request, by sending `confirm` for someone else's thread. | Same as T-02: confirmation proves a request, not who sent it. | accepted (single operator); revisit with auth |
| T-04 | Tampering | Malformed or oversized input reaches the model or a tool. | Pydantic validation at the boundary; message ≤ 4000 chars, `max_tool_calls` 1..20 (SPEC-0002 B-0001, SPEC-0012 B-0001). | mitigated |
| T-05 | Info disclosure | Errors leak stack traces, exception text or keys. | Fixed error bodies; provider errors name the cause, never a key (SPEC-0002 B-0005, B-0007, B-0008). | mitigated |
| T-06 | Info disclosure | A secret the model saw is returned to the client. | Configured secrets are replaced with `[redacted]` in answers, thread views and summaries (SPEC-0012 B-0003); `test_no_route_returns_a_secret` sweeps every route. | partly: `pending_action.input` and tool call records are not redacted (SPEC-0012 B-0003 leaves tool call records unchanged); [#69](https://github.com/darrenpaine/harnessd/issues/69) |
| T-07 | Denial of service | A caller floods the API, or holds threads busy. | One run per thread (SPEC-0004 B-0005); run timeout (SPEC-0001 B-0006). No per-client request limit. | accepted: a per-client limit needs a caller identity, so it waits for authentication (SPEC-0013) |
| T-08 | Denial of service | A caller runs up provider cost. | See LLM10. | partly |
| T-09 | Repudiation | No record of who did what. | Audit events for every run, tool result, confirmation, guard action and budget stop, with thread and request id (SPEC-0008 B-0007, SPEC-0012 B-0013, SPEC-0010 B-0006). They record what happened, not who: there is no caller identity. | partly |
| T-10 | Info disclosure | `/docs`, `/redoc` and `/openapi.json` describe the whole API to anyone, and Swagger assets load from a CDN. | Intended for an example service; stays open under SPEC-0013 B-0001. | accepted |

### TB2: API → Redis

| Id | STRIDE | Threat | Mitigation | Status |
|----|--------|--------|------------|--------|
| T-11 | Info disclosure, Tampering | Redis has no password or TLS. Anyone who reaches it can read every thread, edit checkpoints, or reset the day's spend. | Compose publishes it on 127.0.0.1 only, so only processes on the same machine reach it ([#66](https://github.com/darrenpaine/harnessd/issues/66)). SPEC-0013 B-0003 uses `rediss://` with a managed Redis. | mitigated locally; planned for deployment |
| T-12 | Info disclosure | Checkpoints hold raw message text, including any secret a user or page put there; redaction happens only on the way out. | Deliberate: the checkpoint never leaves the server (SPEC-0012 B-0003). Threads expire after `CHECKPOINT_TTL_HOURS` (SPEC-0004 B-0003) and can be deleted (SPEC-0009 B-0002). | accepted |
| T-13 | Tampering | A crafted or corrupted checkpoint breaks every request on a thread. | DELETE removes a thread without loading its state (SPEC-0009 B-0002). An interrupted confirmation no longer leaves a thread unusable (SPEC-0004 B-0007). | mitigated for known cases |
| T-14 | Denial of service | Redis is down, slow, or its keyspace is large. | 503 with no stack trace (SPEC-0002 B-0005). The TTL refresh scans the whole keyspace ([#14](https://github.com/darrenpaine/harnessd/issues/14); SPEC-0013 B-0006). | partly |
| T-15 | Elevation | Two runs on one thread at once corrupt its state or repeat a side effect. | Redis lock per thread, expiring after the run timeout + 30 s (SPEC-0004 B-0005). | mitigated |

### TB3: Agent → model provider

| Id | STRIDE | Threat | Mitigation | Status |
|----|--------|--------|------------|--------|
| T-16 | Info disclosure | Everything in a thread, including fetched pages and anything the user pasted, is sent to a third-party provider. | The operator chooses the provider; Ollama keeps it local. Prompts are built in one function (`build_prompt`). Secrets from settings are never put in a prompt. | accepted (documented) |
| T-17 | Spoofing, Tampering | A reply is malformed, or asks for a tool or arguments that do not exist. | Every tool call is validated against its Input model; an invalid turn is fed back once, then the run ends (SPEC-0001 B-0004, SPEC-0003 B-0001). | mitigated |
| T-18 | Info disclosure | A provider key leaks through an error, a log or a span. | SPEC-0002 B-0007, SPEC-0008 B-0004, SPEC-0010 B-0004. | mitigated |

### TB4: Tools → internet

| Id | STRIDE | Threat | Mitigation | Status |
|----|--------|--------|------------|--------|
| T-19 | Elevation (SSRF) | The model, steered by a user or a page, fetches an internal address: cloud metadata, Redis, Langfuse. | fetch_site rejects private, loopback and link-local addresses, also after each redirect; at most 3 redirects; http(s) only (SPEC-0003 B-0006). | partly: see T-20 |
| T-20 | Elevation (SSRF) | DNS rebinding: the address is checked, then `httpx` resolves again on connect and may get an internal one. | None. Fix: connect to the checked IP. | open: [#71](https://github.com/darrenpaine/harnessd/issues/71) |
| T-21 | Denial of service | A huge or slow response ties up a run. | 10 s tool timeout, 1 MB body cap, text content only, 1000 chars returned (SPEC-0003 B-0002, B-0006). | mitigated |
| T-22 | Info disclosure | The model puts thread contents or a secret into a URL or search query, sending it to an outside server. | See LLM02 (b). | accepted for now |

### TB5: Tool results → model

Covered under LLM01 below; it is the main threat to this service.

### TB6: Agent → notes file

| Id | STRIDE | Threat | Mitigation | Status |
|----|--------|--------|------------|--------|
| T-23 | Tampering | The model writes a note the user did not ask for. | Every add_note pauses for a human decision; the decision is saved before the tool runs (SPEC-0001 B-0005). | mitigated |
| T-24 | Tampering | Forged or multi-line entries, or a write outside the notes file. | Text is one line, 1..500 chars; the path comes from settings, never from the model (SPEC-0003 B-0007). | mitigated |
| T-25 | Tampering | A note is written twice. | A completed side effect is never repeated (SPEC-0003 B-0004); a run stopped during a confirmed call does not rerun it (SPEC-0004 B-0007). A tool timeout can still report `timeout` after the write landed, and the model may ask again, which needs a second confirmation. | partly |
| T-26 | Denial of service | The notes file grows without bound. | Each note needs a confirmation. SPEC-0013 B-0007 caps the Redis backend at 1000 entries. | accepted |

### TB7: App → logs and traces

| Id | STRIDE | Threat | Mitigation | Status |
|----|--------|--------|------------|--------|
| T-27 | Info disclosure | Logs hold message text, answers, tool outputs or keys. | No level logs them; tool inputs only at `debug`, cut to 200 chars (SPEC-0008 B-0004). Audit events are content-free (SPEC-0012 B-0013). | mitigated |
| T-28 | Info disclosure | Traces hold full prompts, replies and tool I/O, sent to whatever OTLP endpoint is configured. `TRACE_CONTENT` defaults to true. | `TRACE_CONTENT=false` removes content from spans; keys never appear (SPEC-0010 B-0004). The local Langfuse from compose is published on 127.0.0.1 only. | accepted while the service is under development and testing: content stays on by default because the traces are used to debug runs, and secrets inside message text are not redacted in spans ([#68](https://github.com/darrenpaine/harnessd/issues/68)) |
| T-29 | Tampering (log injection) | A client-supplied `X-Request-ID` forges log lines. | Ids that break the format are replaced (SPEC-0010 B-0007). | mitigated |
| T-30 | Repudiation | Audit lines are lost or altered. | They go to stdout with the other logs; there is no separate, tamper-evident store. | accepted |

### TB8: Build and supply chain

| Id | STRIDE | Threat | Mitigation | Status |
|----|--------|--------|------------|--------|
| T-31 | Tampering | A compromised or vulnerable dependency. | Versions pinned in `uv.lock`; new dependencies need approval (CLAUDE.md). CI audits every locked package, dev ones included, with pip-audit and fails on a known vulnerability not listed in `.github/pip-audit-ignore.txt`; Dependabot proposes weekly updates for uv, GitHub Actions, the Dockerfile's base image and pre-commit hooks ([#70](https://github.com/darrenpaine/harnessd/issues/70)). | partly: a new advisory against an unchanged lock is seen only on the next push or through Dependabot alerts, as nothing audits on a schedule; the uv image the Dockerfile copies from is `latest`, which Dependabot cannot track |
| T-32 | Elevation | The container runs as root, so a code-execution bug has the whole container and its volumes. | The app runs as the unprivileged user `app` ([#67](https://github.com/darrenpaine/harnessd/issues/67)); it cannot change the app's code or installed packages, which root owns. | mitigated |
| T-33 | Info disclosure | A secret is committed. | `.env` is gitignored; tests clear secret environment variables. gitleaks scans staged changes in a pre-commit hook and pushed commits in CI ([#70](https://github.com/darrenpaine/harnessd/issues/70)); findings that are not secrets are listed in `.gitleaksignore`. | mitigated for known key formats: the hook runs only where `make pre-commit-setup` was run, so a secret can reach a pushed branch before CI flags it |
| T-34 | Tampering | `ShallowRedisSaver` overrides a private method of `langgraph-checkpoint-redis`; an upgrade can change behavior silently. | Import fails if the method goes; SPEC-0004 AT-0006 fails if it stops applying. | mitigated |

## OWASP Top 10 for LLM Applications (2025)

| Id | Risk | How it applies here | Mitigation | Status |
|----|------|---------------------|------------|--------|
| LLM01 | Prompt injection | Indirect: a fetched page or search result tells the agent to do something. Direct injection by the user is out of scope, as the user is the principal. | Layers: results reach the model only as tool messages, and the system prompt calls them untrusted (SPEC-0012 B-0002); invisible characters and hidden HTML are removed (B-0015, B-0016); results are capped (B-0001); an optional injection screen withholds flagged results (B-0005, B-0006). What an injection can *do* is bounded by LLM06. | partly: the screen is off unless `INJECTION_SCREEN` is set, it is probabilistic, and `INJECTION_SCREEN_ON_ERROR=allow` passes results when it fails |
| LLM02 | Sensitive information disclosure | (a) A secret appears in an answer. (b) An injected page makes the agent send thread contents out through a fetch_site URL or a web_search query. (c) Thread contents reach traces or the provider. | (a) Redaction (SPEC-0012 B-0003; T-06). (b) No egress control: read-only tools run without confirmation, so a GET to an attacker's URL is not stopped. Configured secrets are not in the model's context, which limits what can leak to thread contents. (c) T-16, T-28. | (a) partly, (b) accepted for now: stopping it needs an allow-list of hosts or a confirmation for every fetch, which would defeat the tool in a single-operator example; reopen when the service is exposed, (c) accepted / partly |
| LLM03 | Supply chain | Python dependencies, the base image, hosted models, and local models pulled through Ollama. | T-31. Models are named in settings by the operator. | partly |
| LLM04 | Data and model poisoning | No training or fine-tuning. A poisoned page can enter a thread and, through compaction, its summary, which later runs receive in the system prompt (SPEC-0009 B-0005). | Results are screened and stripped before they enter the thread (LLM01). The summary is not screened. | partly |
| LLM05 | Improper output handling | Model output is returned as JSON and written to a text file. It is never executed, rendered as HTML by the service, or used to build a query or a shell command. | Responses validate against `AgentResponse` (SPEC-0002 B-0002); tool arguments against Input models (SPEC-0003 B-0001). A client that renders an answer as HTML must escape it. | mitigated in the service |
| LLM06 | Excessive agency | The agent has three tools; one has a side effect. | Narrow tools; the only side effect needs a human decision each time (SPEC-0001 B-0005); tool calls per run are capped in code (B-0002); a run has a timeout (B-0006). Read-only tools need no confirmation, which is what leaves LLM02 (b) open. | mitigated for side effects |
| LLM07 | System prompt leakage | The system prompt can be extracted by asking. | It holds no secret and no access rule; it is in the repository (`src/prompts/`). Controls are in code, not the prompt. | accepted |
| LLM08 | Vector and embedding weaknesses | No retrieval store or embeddings. | n/a | n/a |
| LLM09 | Misinformation | The agent may state wrong facts or misreport a source. | `tool_calls` in every response shows what was actually fetched. Answer quality is measured in evals (SPEC-0011), not enforced. | accepted |
| LLM10 | Unbounded consumption | A caller, or a loop the model falls into, runs up tokens and cost. | Tool-call cap and run timeout (SPEC-0001 B-0002, B-0006); run and thread token budgets (SPEC-0012 B-0009, B-0014); optional daily cost limit (B-0010, B-0011); provider call pacing (SPEC-0001 B-0008). | partly: the daily limit is off unless set; pacing is per process; concurrent runs can each exceed the limit by one call; no per-client limit (T-07); history is bounded only by the thread budget and manual compaction |

## Open items

The threats waiting for a fix or a decision, most serious first. Each has an
issue; a threat with no planned work is marked accepted in its row above, with the
reason.

| Threat | What is missing | Issue |
|--------|-----------------|-------|
| T-20 | DNS rebinding in fetch_site. | [#71](https://github.com/darrenpaine/harnessd/issues/71) |
| T-06 | `pending_action.input` and tool call records are returned unredacted. | [#69](https://github.com/darrenpaine/harnessd/issues/69) |

## Revisit when

- Authentication is added (SPEC-0013): T-01, T-02, T-03, T-09 change from accepted
  to needing per-caller thread ownership.
- A tool is added: add its boundary, and check LLM02 and LLM06 for it. A second
  side-effecting tool, or one that takes longer than the run timeout, reopens T-25.
- Development and testing end, or traces go to a backend the operator does not
  run: T-28 (set `TRACE_CONTENT=false` by default, or redact span content).
- The service is exposed beyond its operator: every "accepted (single operator)"
  row is reopened, and so are T-22 and LLM02 (b).
- A retrieval store or memory across threads is added: LLM04 and LLM08.
