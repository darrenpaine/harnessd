# Requirements: harnessd back-end

Status: draft
Owner: github.com/darrenpaine

## Context
Demonstrate AI-agent coding and AI-based programming, including:
- foundation models and best-practice (evals, observability, guardrails)
- back-end API with provider-agnostic LLM usage
- LLM selection of tools, and user approval of some actions
- deployment to cloud services

## Requirements
R-0001, R-0002, R-0003 and R-0024 are about how the project is built, not what the
service does. They are checked by inspection and CI rather than by behavior tests.

| Id     | Requirement                               | Priority (must/should/could) | Source / rationale |
|--------|-------------------------------------------|------------------------------|--------------------|
| R-0001 | The repository MUST include an AI coding harness: agent instructions (`CLAUDE.md`), the spec-first and branch/PR workflow, and the commands an assistant runs to check its work. | must | The project's purpose is a reference harness for AI-assisted development. Checked by inspection. |
| R-0002 | The repository SHOULD document the requirements, design and specifications. | should | Reviewers can follow why and how the system was built. |
| R-0003 | The repository MUST trace each requirement to the spec behaviors that meet it and the tests that prove them. | must | Shows spec-first development; gaps are found by a script, not by reading. |
| R-0004 | The system MUST report its health status without calling its dependencies. | must | Liveness checks for local and cloud running. |
| R-0005 | The system MUST let a chat be started and continued, one turn at a time. | must | Core chat use. |
| R-0006 | The system MUST keep an idle chat for a configured time, then expire it. | must | Bounds storage; chats are not kept forever. |
| R-0007 | The system MUST let the model choose and call a fixed set of tools, such as web search, page fetching and note-taking, over several steps. | must | Demonstrates LLM tool selection. |
| R-0008 | The system MUST ask the user to confirm before any tool with a side effect runs (e.g. writing a note). | must | Human oversight of actions. |
| R-0009 | The system COULD list all current chats. | could | Convenience for a demo; needs an index of chats, and per-user limits once users exist (see Constraints). |
| R-0010 | The system SHOULD return a chat's history. | should | For demonstration, even though a chat may be shared; limited per user once users exist (see Constraints). |
| R-0011 | The system MUST publish interactive API documentation generated from the code. | must | The API can be explored and tried without reading source. |
| R-0012 | The system MUST log its actions without secrets or chat content. | must | Debugging without leaking data. |
| R-0013 | The system MUST keep chats, including a pending confirmation, across a service restart. | must | A restart or redeploy does not lose conversations. |
| R-0014 | The system MUST produce a trace per request showing each model call (with token counts) and tool call, exported with OpenTelemetry. | must | Industry-standard LLM observability. |
| R-0015 | The system MUST provide a scored eval suite that runs against a chosen model and compares results with a per-model baseline. | must | Catches quality regressions from prompt, model or code changes. |
| R-0016 | The system MUST treat tool output as untrusted: content fetched from the web cannot make the agent run a side-effecting tool or leak secrets. | must | Prompt-injection defence. |
| R-0017 | The system SHOULD be deployable to the cloud by an automated pipeline. | should | Repeatable deploys without manual steps. |
| R-0018 | The system SHOULD allow user creation, login and logout. | should | Needed before chats can be private to a user. |
| R-0019 | The system SHOULD allow an admin to manage users. | should | Split from thread management (R-0027). |
| R-0020 | The system COULD provide more tools and let the user choose which are enabled. | could | |
| R-0021 | The system COULD provide access to MCP servers, with the necessary safeguards, and let the user choose which are enabled. | could | |
| R-0022 | The system COULD stream a reply's tokens as the model produces them. | could | |
| R-0023 | The system COULD keep messages confidential, end to end and at rest. | could | |
| R-0024 | Development MUST run automated lint, type and test checks on every change, and a change merges only when they pass. | must | Best-practice development. Checked by the CI configuration. |
| R-0025 | The system MUST let the LLM provider and model be chosen by configuration, with no code change. | must | Provider-agnostic LLM usage (Context). |
| R-0026 | The system MUST return validated, structured responses, and structured errors that hold no stack traces or secrets. | must | Clients can rely on the response shape. |
| R-0027 | The system SHOULD let a chat be deleted, or compacted into a summary when it grows long. | should | Bounds model context and storage. |
| R-0028 | The system MUST limit model spending: tokens per run and per chat, and total cost per day. | must | Real money is spent once deployed. |

## Constraints
- Performance: fast and robust enough to serve as a live demo. The service adds at
  most 200 ms at p95 on top of model and tool time, and handles at least 5 runs at
  once.
- Maintainability: strict typing, linting, at least 80% branch coverage, and spec ids
  on tests.
- Cost: an idle cloud deployment costs about $0-5 a month, and model spend is capped
  per day (R-0028).
- Security: no secrets in the repository, logs or responses. Once users exist
  (R-0018), a user can list and read only their own chats (R-0009, R-0010).

## Out of scope
Multi-agent routing, a user interface, high availability and multi-region
deployment.

## Traceability
| Requirement | Specs |
|-------------|-------|
| R-0001 | `CLAUDE.md`, `.github/workflows/` |
| R-0002 | `docs/requirements/`, `docs/design/`, `docs/specs/` |
| R-0003 | SPEC-0006 |
| R-0004 | SPEC-0002 B-0006 |
| R-0005 | SPEC-0002, SPEC-0004 B-0001, B-0002 |
| R-0006 | SPEC-0004 B-0003 |
| R-0007 | SPEC-0001, SPEC-0003 |
| R-0008 | SPEC-0001 B-0005 |
| R-0009 | none yet |
| R-0010 | SPEC-0009 B-0001 |
| R-0011 | SPEC-0007 |
| R-0012 | SPEC-0008 |
| R-0013 | SPEC-0004 B-0001, B-0004 |
| R-0014 | SPEC-0010 |
| R-0015 | SPEC-0011 |
| R-0016 | SPEC-0012 |
| R-0017 | SPEC-0013 |
| R-0018, R-0019, R-0020..R-0023 | none yet |
| R-0024 | SPEC-0005, `.github/workflows/ci.yaml` |
| R-0025 | SPEC-0001 B-0001 |
| R-0026 | SPEC-0002 B-0002, B-0005, B-0007, B-0008 |
| R-0027 | SPEC-0009 B-0002, B-0004 |
| R-0028 | SPEC-0012 B-0009..B-0011, B-0014 |

Spec-to-test links are generated in the [traceability document](../traceability.md).
