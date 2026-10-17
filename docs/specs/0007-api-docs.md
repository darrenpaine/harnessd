# SPEC-0007: API documentation

Status: implemented
Requirements: R-0011
Design: [design.md](../design/design.md)

## Goal
Interactive, accurate API documentation: a Swagger UI to explore and try the
endpoints in SPEC-0002, backed by an OpenAPI schema that is committed to the repo so
every API change shows up as a reviewable diff.

## Inputs / Outputs
### Endpoints (served by FastAPI, always on)
| Path | Content |
|------|---------|
| `GET /docs` | Swagger UI |
| `GET /redoc` | ReDoc |
| `GET /openapi.json` | OpenAPI 3.1 schema |

The UI pages load their JavaScript from FastAPI's default CDN, so the browser needs
internet access.

### Schema metadata
- title: `harnessd`
- version: `[project].version` from `pyproject.toml`
- description: one paragraph saying what the service does, with a link to
  `docs/specs/README.md`.

### Operations
| Endpoint | Tag | operationId | Responses documented |
|----------|-----|-------------|----------------------|
| `POST /v1/agent/run` | agent | `run_agent` | 200 AgentResponse, 409 ApiError, 422 HTTPValidationError, 500 ApiError, 503 ApiError |
| `GET /health` | health | `health_check` | 200 `{"status": "ok"}` |

ApiError is the 409/500/503 body from SPEC-0002, as a named model:
`{"error": {"code": str, "message": str}}`.

### Committed schema
- File: `docs/openapi.json` (2-space indent, sorted keys, trailing newline).
- Regenerate with `make openapi`, which runs `scripts/export_openapi.py`.

## Behavior
- B-0001: `/docs` serves Swagger UI and `/redoc` serves ReDoc, both reading
  `/openapi.json`. They are always on; there is no setting to disable them.
- B-0002: The schema's title, version and description match the metadata above; the
  version is read from `pyproject.toml`, not repeated in code.
- B-0003: Every operation has a summary, a description, exactly one tag, and the
  operationId in the table above. operationIds are unique.
- B-0004: Every response code an endpoint can return (SPEC-0002 status table) is
  declared with its body schema. ApiError appears as a named schema component.
- B-0005: Every field of AgentRequest and AgentResponse has a description.
  AgentRequest has named examples, offered in Swagger UI's example list, for a new
  conversation, continuing a thread (`thread_id` with `message`) and a confirmation.
  AgentResponse has one example per status. Every example validates against its
  model.
- B-0006: `docs/openapi.json` equals the schema the app generates. Any API change must
  regenerate it in the same PR.

## Acceptance tests
- AT-0001 (B-0001): `GET /docs` and `GET /redoc` → 200 HTML that references
  `/openapi.json`. -> `tests/test_api_docs.py::test_docs_pages_served`
- AT-0002 (B-0002): Schema info has title `harnessd`, the pyproject version and a
  description. -> `tests/test_api_docs.py::test_schema_metadata`
- AT-0003 (B-0003): Every operation has a summary, description, one tag and the
  expected unique operationId. -> `tests/test_api_docs.py::test_operations_documented`
- AT-0004 (B-0004): `run_agent` declares 200/409/422/500/503 with the right schemas;
  ApiError is in `components.schemas`.
  -> `tests/test_api_docs.py::test_error_responses_documented`
- AT-0005 (B-0005): Every AgentRequest/AgentResponse field has a description; each
  model example validates against its model; AgentResponse examples cover all four
  statuses; the run_agent request body offers the three named AgentRequest examples.
  -> `tests/test_api_docs.py::test_model_examples_validate`,
  `tests/test_api_docs.py::test_request_examples_offered`
- AT-0006 (B-0006): `docs/openapi.json` equals `app.openapi()`.
  -> `tests/test_api_docs.py::test_committed_schema_up_to_date`

## Non-goals
Hosting the docs outside the service, self-hosting the Swagger UI assets, auth on the
docs pages, generated client SDKs.

## Open questions
None.
