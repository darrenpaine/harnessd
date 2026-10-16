# CLAUDE.md

## Project
A FastAPI service exposing a single LangGraph agent (with 3 tools) that returns
validated, structured responses. Conversation state is checkpointed in Redis.
Specs live in `docs/specs/`; `docs/specs/README.md` lists them with their status.
B and AT ids are numbered per spec, so always write them with the spec id
(e.g. SPEC-0002 B-0003); see SPEC-0006.

## Stack
- Python 3.14, managed with `uv` (use `uv add` / `uv sync`, never bare pip)
- FastAPI + Uvicorn
- LangGraph (agent graph) with a Redis checkpointer
- Pydantic v2 for every boundary: API request/response and tool I/O
- pytest (+ pytest-asyncio + coverage), Ruff (lint + format), mypy (type check)
- Docker / docker compose (app + Redis), GitHub Actions CI

## Commands
Run these before saying a task is done.

| Task           | Command                                                    |
|----------------|------------------------------------------------------------|
| Install        | `uv sync`                                                  |
| Run (local)    | `docker compose up -d` (app + Redis)                       |
| Redis only     | `docker compose up -d redis` (needed by `redis` tests)     |
| Run (dev)      | `uv run uvicorn app:app --app-dir src --reload --port 5999` |
| Tests/Coverage | `uv run pytest --cov=src ./`                               |
| One test       | `uv run pytest tests/path.py::test_name`                   |
| Lint           | `uv run ruff check .`                                      |
| Format         | `uv run ruff format .`                                     |
| Types          | `uv run mypy src/ tests/ scripts/`                         |
| OpenAPI schema | `make openapi` (after any API change; commit `docs/openapi.json`) |

CI runs lint, types, and tests on every push. Keep all three green. Check this locally using: `uv run pre-commit run --all-files`

## Workflow: spec before code
1. Before writing or changing behavior, read the relevant file in `docs/specs/`.
2. If the behavior is not in a spec, or the spec is wrong, update the spec
   first and ask me to review it. Do not code against an unreviewed spec.
3. Define or update Pydantic models in `src/schemas.py` before the logic that uses them.
4. Write the acceptance tests listed in the spec, then implement until they pass.
5. Reference spec ids in tests (e.g. `@pytest.mark.spec("SPEC-0001", "B-0002")`) so
   requirements stay traceable to tests.

## Workflow: branches and pull requests
- Never commit directly to `main`. Start each feature or fix on a new branch from
  an up-to-date `main`: `git switch main && git pull && git switch -c <type>/<short-name>`
  (types: `feat/`, `fix/`, `docs/`, `chore/`; e.g. `feat/settings`).
- One branch per spec slice or concern. Keep the "small commits" rule within the branch.
- Before opening a PR, run `uv run pre-commit run --all-files` and the full test suite.
- Push with `git push -u origin <branch>` and open a PR to `main` with `gh pr create`.
  The PR description lists the spec ids it covers (e.g. SPEC-0001 B-0004, AT-0006).
  Do not add a "Generated with Claude Code" line or any other Claude attribution.
- Merge only after CI is green and I've approved. I merge; don't merge your own PR
  unless I ask. Delete the branch after merge.

## Layout
```
src/
  app.py           # FastAPI app and routes
  schemas.py       # Pydantic models (API + tool I/O)
  agent/           # LangGraph graph, state, checkpointer
  tools/           # one module per tool, each with typed input/output
docs/
  design/          # how it is built: components, data flow, trade-offs
  requirements/    # what the system must do (R-nnnn statements)
  specs/           # per-feature contract: goal, I/O, behavior, acceptance 
tests/
Dockerfile
docker-compose.yml
pyproject.toml
uv.lock            # UV sync file
```

## Conventions
- Type annotation hints everywhere - in src and tests; no untyped functions or parameters.
- Tools take and return Pydantic models. A tool failure returns a structured
  error to the model; it never raises into the graph.
- No real LLM calls in tests. Use a fake or scripted model so CI is deterministic.
- Use dependency injection to insert graph object in FastAPI call handlers.
- Tests that need Redis use the compose service or a fixture, not mocks of the checkpointer.
- Config comes from environment variables via a single settings class. Never
  commit secrets; `.env` is gitignored, `.env.example` is committed.
- Small commits with imperative messages, one concern each.
- Commit comments should be concise, simple language. Do not add 'co-author'.
- Ask me before adding dependencies.

## Non-goals (for now)
Auth, streaming, multi-agent routing, a UI, production deployment.
