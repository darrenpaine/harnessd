# Threat model

What could go wrong with harnessd from a security point of view, what stops it, and
what is left open. It uses STRIDE over each trust boundary, then three OWASP
lists for the threats STRIDE has no natural place for: the Top 10 for LLM
Applications 2026, the Top 10 for Agentic Applications 2026 (the agent chooses
its own tools), and selected GenAI Data Security 2026 risks. OWASP ids carry
their edition (`LLM03:2026`), because the 2025 list numbered the same risks
differently.

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
| n/a | Does not apply, with the reason. |

## Scope and assumptions

- harnessd is an example service, run locally with docker compose. Production
  deployment is a non-goal for now. SPEC-0013 (reviewed, not built) describes a
  cloud demo: one shared API key, with everyone holding it trusted like the
  operator. Rows that change in the cloud say so.
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
| T-01 | Spoofing | Any caller can use the API as if they were the operator. | No authentication (a non-goal). Compose publishes the API on 127.0.0.1 only, so callers must be on the same machine. In the cloud, SPEC-0013 B-0001 requires a shared `X-API-Key`; key holders are one trusted group. | planned; accepted locally (non-goal) |
| T-02 | Info disclosure, Tampering | A caller who knows a thread id can read, continue, compact or delete that thread. Ids are UUIDs, but a client may choose its own, and they appear in logs and traces. | Server-made ids are uuid4. No per-thread owner. In the cloud, unknown ids are refused, so ids can't be chosen or guessed (SPEC-0013 B-0003). | accepted (single operator; in the cloud, the trusted key holders); revisit with per-user identity (R-0018) |
| T-03 | Elevation | A caller approves a pending action they did not request, by sending `confirm` for someone else's thread. | Same as T-02: confirmation proves a request, not who sent it. | accepted (single operator; in the cloud, the trusted key holders); revisit with per-user identity (R-0018) |
| T-04 | Tampering | Malformed or oversized input reaches the model or a tool. | Pydantic validation at the boundary; message ≤ 4000 chars, `max_tool_calls` 1..20 (SPEC-0002 B-0001, SPEC-0012 B-0001). | mitigated |
| T-05 | Info disclosure | Errors leak stack traces, exception text or keys. | Fixed error bodies; provider errors name the cause, never a key (SPEC-0002 B-0005, B-0007, B-0008). | mitigated |
| T-06 | Info disclosure | A secret the model saw is returned to the client. | Configured secrets are replaced with `[redacted]` in answers, thread views and summaries (SPEC-0012 B-0003); `test_no_route_returns_a_secret` sweeps every route. | partly: `pending_action.input` and tool call records are not redacted (SPEC-0012 B-0003 leaves tool call records unchanged); [#69](https://github.com/darrenpaine/harnessd/issues/69) |
| T-07 | Denial of service | A caller floods the API, or holds threads busy. | One run per thread (SPEC-0004 B-0005); run timeout (SPEC-0001 B-0006). No per-client request limit. In the cloud, the Lambda's reserved concurrency caps parallel runs at 5 (SPEC-0013). | accepted: a per-client limit needs a caller identity, so it waits for per-user identity (R-0018) |
| T-08 | Denial of service | A caller runs up provider cost. | See LLM06:2026. | partly |
| T-09 | Repudiation | No record of who did what. | Audit events for every run, tool result, confirmation, guard action and budget stop, with thread and request id (SPEC-0008 B-0007, SPEC-0012 B-0013, SPEC-0010 B-0006). They record what happened, not who: there is no caller identity. | partly |
| T-10 | Info disclosure | `/docs`, `/redoc` and `/openapi.json` describe the whole API to anyone, and Swagger assets load from a CDN. | Intended for an example service; stays open under SPEC-0013 B-0001. | accepted |

### TB2: API → Redis

| Id | STRIDE | Threat | Mitigation | Status |
|----|--------|--------|------------|--------|
| T-11 | Info disclosure, Tampering | Redis has no password or TLS. Anyone who reaches it can read every thread, edit checkpoints, or reset the day's spend. | Compose publishes it on 127.0.0.1 only, so only processes on the same machine reach it ([#66](https://github.com/darrenpaine/harnessd/issues/66)). In the cloud (SPEC-0013), the free Redis Cloud database has a long random password but no TLS, so the password and threads cross the network in clear; it is in the Lambda's AWS region, so the traffic stays inside AWS's network. | mitigated locally; accepted in the cloud for demo data only: key holders are told not to put anything sensitive in a thread; real data or users outside the trusted group means the paid plan with `rediss://` (SPEC-0014 B-0001) |
| T-12 | Info disclosure | Checkpoints hold raw message text, including any secret a user or page put there; redaction happens only on the way out. | Deliberate: the checkpoint never leaves the server (SPEC-0012 B-0003). Threads expire after `CHECKPOINT_TTL_HOURS` (SPEC-0004 B-0003) and can be deleted (SPEC-0009 B-0002). | accepted |
| T-13 | Tampering | A crafted or corrupted checkpoint breaks every request on a thread. | DELETE removes a thread without loading its state (SPEC-0009 B-0002). An interrupted confirmation no longer leaves a thread unusable (SPEC-0004 B-0007). | mitigated for known cases |
| T-14 | Denial of service | Redis is down, slow, or its keyspace is large. | 503 with no stack trace (SPEC-0002 B-0005). The TTL refresh scans the whole keyspace ([#14](https://github.com/darrenpaine/harnessd/issues/14)). SPEC-0014 adds retries, rebuilding a broken connection (B-0002, B-0003) and a refresh without SCAN (B-0004). | partly; planned (SPEC-0014, reviewed) |
| T-15 | Elevation | Two runs on one thread at once corrupt its state or repeat a side effect. | Redis lock per thread, expiring after the run timeout + 30 s (SPEC-0004 B-0005). | mitigated |
| T-35 | Denial of service | In the cloud, the free Redis database is deleted after 14 days with no commands, with every thread and note in it. | Keeping it in use is the operator's job, outside this repository (SPEC-0013). | accepted: demo data only |

### TB3: Agent → model provider

| Id | STRIDE | Threat | Mitigation | Status |
|----|--------|--------|------------|--------|
| T-16 | Info disclosure | Everything in a thread, including fetched pages and anything the user pasted, is sent to a third-party provider. | The operator chooses the provider; Ollama keeps it local. Prompts are built in one function (`build_prompt`). Secrets from settings are never put in a prompt. | accepted (documented) |
| T-17 | Spoofing, Tampering | A reply is malformed, or asks for a tool or arguments that do not exist. | Every tool call is validated against its Input model; an invalid turn is fed back once, then the run ends (SPEC-0001 B-0004, SPEC-0003 B-0001). | mitigated |
| T-18 | Info disclosure | A provider key leaks through an error, a log or a span. | SPEC-0002 B-0007, SPEC-0008 B-0004, SPEC-0010 B-0004. | mitigated |

### TB4: Tools → internet

| Id | STRIDE | Threat | Mitigation | Status |
|----|--------|--------|------------|--------|
| T-19 | Elevation (SSRF) | The model, steered by a user or a page, fetches an internal address: cloud metadata, Redis, Langfuse. | fetch_site rejects private, loopback and link-local addresses, also after each redirect; at most 3 redirects; http(s) only (SPEC-0003 B-0006). | mitigated |
| T-20 | Elevation (SSRF) | DNS rebinding: the address is checked, then the HTTP client resolves the name again on connect and may get an internal one. | fetch_site connects to the address it checked, for the first request and each redirect, and never resolves the name again (SPEC-0003 B-0006; [#71](https://github.com/darrenpaine/harnessd/issues/71)). | mitigated |
| T-21 | Denial of service | A huge or slow response ties up a run. | 10 s tool timeout, 1 MB body cap, text content only, 1000 chars returned (SPEC-0003 B-0002, B-0006). | mitigated |
| T-22 | Info disclosure | The model puts thread contents or a secret into a URL or search query, sending it to an outside server. | See LLM02:2026 (b). | accepted for now |

### TB5: Tool results → model

Covered under LLM01:2026 below; it is the main threat to this service.

### TB6: Agent → notes file

| Id | STRIDE | Threat | Mitigation | Status |
|----|--------|--------|------------|--------|
| T-23 | Tampering | The model writes a note the user did not ask for. | Every add_note pauses for a human decision; the decision is saved before the tool runs (SPEC-0001 B-0005). | mitigated |
| T-24 | Tampering | Forged or multi-line entries, or a write outside the notes file. | Text is one line, 1..500 chars; the path comes from settings, never from the model (SPEC-0003 B-0007). | mitigated |
| T-25 | Tampering | A note is written twice. | A completed side effect is never repeated (SPEC-0003 B-0004); a run stopped during a confirmed call does not rerun it (SPEC-0004 B-0007). A tool timeout can still report `timeout` after the write landed, and the model may ask again, which needs a second confirmation. | partly |
| T-26 | Denial of service | The notes file grows without bound. | Each note needs a confirmation. SPEC-0013 B-0004 caps the Redis backend at 1000 entries. | accepted |

### TB7: App → logs and traces

| Id | STRIDE | Threat | Mitigation | Status |
|----|--------|--------|------------|--------|
| T-27 | Info disclosure | Logs hold message text, answers, tool outputs or keys. | No level logs them; tool inputs only at `debug`, cut to 200 chars (SPEC-0008 B-0004). Audit events are content-free (SPEC-0012 B-0013). In the cloud, logs go to CloudWatch, kept 14 days (SPEC-0013). | mitigated |
| T-28 | Info disclosure | Traces hold full prompts, replies and tool I/O, sent to whatever OTLP endpoint is configured. `TRACE_CONTENT` defaults to true. | `TRACE_CONTENT=false` removes content from spans; keys never appear (SPEC-0010 B-0004). The local Langfuse from compose is published on 127.0.0.1 only. Tracing is off in the cloud (SPEC-0013). | accepted while the service is under development and testing: content stays on by default because the traces are used to debug runs, and secrets inside message text are not redacted in spans ([#68](https://github.com/darrenpaine/harnessd/issues/68)) |
| T-29 | Tampering (log injection) | A client-supplied `X-Request-ID` forges log lines. | Ids that break the format are replaced (SPEC-0010 B-0007). | mitigated |
| T-30 | Repudiation | Audit lines are lost or altered. | They go to stdout with the other logs; there is no separate, tamper-evident store. | accepted |

### TB8: Build and supply chain

| Id | STRIDE | Threat | Mitigation | Status |
|----|--------|--------|------------|--------|
| T-31 | Tampering | A compromised or vulnerable dependency. | Versions pinned in `uv.lock`; new dependencies need approval (CLAUDE.md). CI audits every locked package, dev ones included, with pip-audit and fails on a known vulnerability not listed in `.github/pip-audit-ignore.txt`; Dependabot proposes weekly updates for uv, GitHub Actions, the Dockerfile's base image and pre-commit hooks ([#70](https://github.com/darrenpaine/harnessd/issues/70)). | partly: a new advisory against an unchanged lock is seen only on the next push or through Dependabot alerts, as nothing audits on a schedule; the uv image the Dockerfile copies from is `latest`, which Dependabot cannot track |
| T-32 | Elevation | The container runs as root, so a code-execution bug has the whole container and its volumes. | The app runs as the unprivileged user `app` ([#67](https://github.com/darrenpaine/harnessd/issues/67)); it cannot change the app's code or installed packages, which root owns. | mitigated |
| T-33 | Info disclosure | A secret is committed. | `.env` is gitignored; tests clear secret environment variables. gitleaks scans staged changes in a pre-commit hook and pushed commits in CI ([#70](https://github.com/darrenpaine/harnessd/issues/70)); findings that are not secrets are listed in `.gitleaksignore`. | mitigated for known key formats: the hook runs only where `make pre-commit-setup` was run, so a secret can reach a pushed branch before CI flags it |
| T-34 | Tampering | An override of a private method of `langgraph-checkpoint-redis` could change behavior silently on an upgrade. | The override is gone: the library fixed the underlying bug in 0.6.0, and the app uses its checkpointer as published. SPEC-0004 AT-0006 is the regression test. | mitigated |

## OWASP Top 10 for LLM Applications 2026

| Id | Risk | How it applies here | Mitigation | Status |
|----|------|---------------------|------------|--------|
| LLM01:2026 | Prompt injection | Indirect: a fetched page or search result tells the agent to do something. Direct injection by the user is out of scope, as the user is the principal. Inputs are text only: fetch_site keeps text content types, so there is no image or audio channel. | Layers: results reach the model only as tool messages, and the system prompt calls them untrusted (SPEC-0012 B-0002); invisible characters and hidden HTML are removed (B-0015, B-0016); results are capped (B-0001); an optional injection screen withholds flagged results (B-0005, B-0006). What an injection can *do* is bounded by LLM03:2026. | partly: the screen is off unless `INJECTION_SCREEN` is set, it is probabilistic, and `INJECTION_SCREEN_ON_ERROR=allow` passes results when it fails |
| LLM02:2026 | Sensitive information disclosure | (a) A secret appears in an answer. (b) An injected page makes the agent send thread contents out through a fetch_site URL or a web_search query. (c) Thread contents reach traces or the provider. | (a) Redaction (SPEC-0012 B-0003; T-06). (b) No egress control: read-only tools run without confirmation, so a GET to an attacker's URL is not stopped. Configured secrets are not in the model's context, which limits what can leak to thread contents. (c) T-16, T-28. | (a) partly, (b) accepted for now: stopping it needs an allow-list of hosts or a confirmation for every fetch, which would defeat the tool in a single-operator example; reopen when the service is exposed, (c) accepted / partly |
| LLM03:2026 | Excessive agency | The agent has three tools; one has a side effect. | Narrow tools; the only side effect needs a human decision each time (SPEC-0001 B-0005); tool calls per run are capped in code (B-0002); a run has a timeout (B-0006). Read-only tools need no confirmation, which is what leaves LLM02:2026 (b) open. | mitigated for side effects |
| LLM04:2026 | Supply chain | Python dependencies, the base image, hosted models, and local models pulled through Ollama. | T-31. Models are named in settings by the operator. An Ollama model is named by tag, not digest, so the model behind a tag can change when it is pulled again. | partly |
| LLM05:2026 | Data and model poisoning | No training or fine-tuning. A poisoned page can enter a thread and, through compaction, its summary, which later runs receive in the system prompt (SPEC-0009 B-0005). | Results are screened and stripped before they enter the thread (LLM01:2026). The summary is not screened. | partly |
| LLM06:2026 | Unbounded consumption | A caller, or a loop the model falls into, runs up tokens and cost. Model cloning does not apply: the service hosts no model of its own. | Tool-call cap and run timeout (SPEC-0001 B-0002, B-0006); run and thread token budgets (SPEC-0012 B-0009, B-0014); optional daily cost limit (B-0010, B-0011); provider call pacing (SPEC-0001 B-0008). | partly: the daily limit is off unless set; pacing is per process; concurrent runs can each exceed the limit by one call; no per-client limit (T-07); history is bounded only by the thread budget and manual compaction |
| LLM07:2026 | Misinformation | The agent may state wrong facts or misreport a source. The only action it can take on a wrong belief is add_note, which a human approves first. | `tool_calls` in every response shows what was actually fetched. Answer quality is measured in evals (SPEC-0011), not enforced. | accepted |
| LLM08:2026 | Hidden context exposure | The hidden context is the system prompt, the tool schemas and a compacted thread's summary. It can be extracted by asking. | The system prompt and tool schemas hold no secret and no access rule, and are in the repository (`src/prompts/`, `src/tools/`); controls are in code, not the prompt. The summary is the user's own thread. | accepted |
| LLM09:2026 | Vector and embedding weaknesses | No retrieval store, embeddings or semantic cache. | n/a | n/a |
| LLM10:2026 | Improper output handling | Model output is returned as JSON and written to a text file. It is never executed, rendered as HTML by the service, or used to build a query or a shell command, and no generated code is run. | Responses validate against `AgentResponse` (SPEC-0002 B-0002); tool arguments against Input models (SPEC-0003 B-0001). A client that renders an answer as HTML must escape it. | mitigated in the service |

## OWASP Top 10 for Agentic Applications 2026

The agent is an actor: it chooses which tools to call and when, so these risks
apply alongside the LLM list.

**Capability composition (rule of two).** The agent holds all three legs of the
lethal trifecta: private data (thread contents), untrusted content (fetched
pages and search results), and external communication (fetch_site and
web_search send requests to any public host). The side-effect part of the third
leg, add_note, needs a human decision; the communication part does not. The
residual risk is T-22 and LLM02:2026 (b), accepted by the operator while the
service is used only by its operator or, in the cloud, the trusted key holders
(SPEC-0013). Removing a leg would need an egress
allow-list or a confirmation for every fetch. Status: accepted; reopen when the
service is exposed beyond those groups.

| Id | Risk | How it applies here | Mitigation | Status |
|----|------|---------------------|------------|--------|
| ASI01:2026 | Agent goal hijack | A fetched page or search result redirects the run. | See LLM01:2026; what a hijacked run can do is bounded by LLM03:2026 and the run limits. | partly |
| ASI02:2026 | Tool misuse and exploitation | A read-only tool carries thread contents out (T-22); fetch_site aimed at internal addresses (T-19, T-20); calls outside a tool's schema (T-17). | See those rows. | partly: T-22 accepted |
| ASI03:2026 | Identity and privilege abuse | Tools act with the service's own Tavily key and network position, not the caller's; there is no caller identity. | Keys never enter the model's context (T-16, T-18); internal addresses are refused (T-19, T-20); the notes path comes from settings (T-24); the app runs unprivileged (T-32). | accepted (single operator); revisit with auth |
| ASI04:2026 | Agentic supply chain vulnerabilities | Tools and prompts are code in this repository. No MCP servers, plugins or tool definitions are loaded at run time, so a definition cannot change after review. | T-31; prompt and tool changes are reviewed like code. | partly (T-31) |
| ASI05:2026 | Unexpected code execution | No tool runs code, a shell or a query; model output is never executed (LLM10:2026). | By design. | n/a |
| ASI06:2026 | Memory and context poisoning | Memory is per thread: its history and compaction summary, kept for `CHECKPOINT_TTL_HOURS`. Nothing carries across threads. | See LLM05:2026. | partly: the summary is not screened |
| ASI07:2026 | Insecure inter-agent communication | One agent; it talks to no other agent. The optional injection screen is a classifier whose output is a typed verdict, not an agent. | n/a | n/a |
| ASI08:2026 | Cascading failures | A bad tool call or a failed tool feeds later turns. | Tool failures return structured errors, never exceptions (SPEC-0003 B-0001); two invalid turns in a row end the run (SPEC-0001 B-0004); tool-call cap, timeout and token budgets (SPEC-0001 B-0002, B-0006; SPEC-0012 B-0009). | partly: a well-formed hostile result is limited only by LLM01:2026 |
| ASI09:2026 | Human-agent trust exploitation | The model words its answer to get an add_note approved, or asks again after a rejection. | The pending action shows the exact tool and input that will run, and the decision applies to that call only (SPEC-0001 B-0005). Each add_note needs its own decision, and tool calls per run are capped. | mitigated for what the approver sees; [#69](https://github.com/darrenpaine/harnessd/issues/69) would change it |
| ASI10:2026 | Rogue agents | A run keeps acting outside its limits or unseen. | A run exists only inside a request, with a timeout (SPEC-0001 B-0006), a tool-call cap (B-0002) and budgets (SPEC-0012 B-0009); there is no background work. Runs, tool results and confirmations are audited (SPEC-0012 B-0013) and traced (SPEC-0010 B-0002). A thread can be deleted (SPEC-0009 B-0002). | mitigated |

## OWASP GenAI Data Security 2026 (selected)

Chosen because the service holds conversations, keys and telemetry. Skipped:
DSGAI11:2026 (one operator, so no context shared between users; see T-02),
DSGAI06:2026 (the only data sent to tools is URLs and queries, LLM02:2026 (b)),
and the risks for training, synthetic data, multimodal input, vector stores,
generated queries, browser assistants, labellers and hosted models, none of which
the service has.

| Id | Risk | How it applies here | Mitigation | Status |
|----|------|---------------------|------------|--------|
| DSGAI01:2026 | Sensitive data leakage | Secrets or personal data in a thread reach a client, a tool or the provider. | See LLM02:2026 and T-06. | partly; [#69](https://github.com/darrenpaine/harnessd/issues/69) |
| DSGAI02:2026 | Agent identity and credential exposure | Provider, Tavily and Langfuse keys are held by the app on the agent's behalf. | Never put in a prompt (T-16); never in errors, logs or spans (T-18); not committed (T-33). | mitigated |
| DSGAI07:2026 | Data governance and lifecycle | Threads, summaries, notes, logs and traces each keep data. | Threads expire after `CHECKPOINT_TTL_HOURS` and can be deleted (SPEC-0004 B-0003, SPEC-0009 B-0002). The notes file and traces have no retention set by the service. | accepted: the notes file is the operator's own record, and trace retention is set in the tracing backend |
| DSGAI14:2026 | Excessive telemetry leakage | Prompts, replies and tool I/O in logs and traces. | See T-27 and T-28. | accepted (T-28) |
| DSGAI15:2026 | Over-broad context | Every turn sends the whole thread history, including fetched text, to the provider. | Tool results are capped (SPEC-0003 B-0006, SPEC-0012 B-0001); history is bounded by the thread token budget (SPEC-0012 B-0014) and manual compaction (SPEC-0009 B-0005); settings secrets never enter a prompt. | partly: no automatic history bound |

## Open items

The threats waiting for a fix or a decision, most serious first. Each has an
issue; a threat with no planned work is marked accepted in its row above, with the
reason.

| Threat | What is missing | Issue |
|--------|-----------------|-------|
| T-06 | `pending_action.input` and tool call records are returned unredacted. | [#69](https://github.com/darrenpaine/harnessd/issues/69) |

## Revisit when

- Per-user identity is added (R-0018): T-01, T-02, T-03, T-07, T-09 change from
  accepted to needing per-caller thread ownership and limits.
- The cloud demo holds real data, or users outside the trusted key holders: T-11
  and T-35 (the paid Redis plan with TLS, backups and no idle deletion).
- A tool is added: add its boundary, and check LLM02:2026, LLM03:2026 and the capability
  composition for it. A second
  side-effecting tool, or one that takes longer than the run timeout, reopens T-25.
- Development and testing end, or traces go to a backend the operator does not
  run: T-28 (set `TRACE_CONTENT=false` by default, or redact span content).
- The service is exposed beyond its operator and the trusted key holders: every "accepted (single operator)"
  row is reopened, and so are T-22, LLM02:2026 (b), the capability composition,
  ASI03:2026 and DSGAI11:2026.
- A retrieval store or memory across threads is added: LLM05:2026, LLM09:2026,
  ASI06:2026, DSGAI11:2026 and DSGAI13:2026.
- `pending_action.input` is redacted ([#69](https://github.com/darrenpaine/harnessd/issues/69)): ASI09:2026, as the
  approver would no longer see exactly what will be written.

## Sources

The OWASP risk ids and names above are from the OWASP GenAI Security Project
(genai.owasp.org), licensed under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). How each
applies here, and the mitigations, are this project's own.

- *OWASP Top 10 for LLM Applications 2026*, version 2026 v1.0.
- *OWASP Top 10 for Agentic Applications 2026*, and *Securing Agentic
  Applications V2* for the capability-composition rule.
- *OWASP GenAI Data Security Risks and Mitigations 2026* v1.0.
