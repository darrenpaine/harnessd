# harnessd

![CI Status](https://github.com/darrenpaine/harnessd/actions/workflows/ci.yaml/badge.svg)

**A reference harness for AI-assisted development:
spec-first workflow, traceable tests, and CI, demonstrated on a FastAPI/LangGraph agent service.**
- AI coding base using Claude, with prompts, rules and workflows and specific details
for Python which include strict typing (mypy), linting and formatting (ruff) and
code coverage (pytest/coverage) rules.
- FastAPI service exposing a single LangGraph agent (with 3 tools) that returns
validated, structured responses. Conversation state is checkpointed in Redis.
- Provider-agnostic, allowing different foundation models, currently supporting Anthropic,
Gemini and local Ollama models.
- Specs live in `docs/specs/`; see the [spec index](docs/specs/README.md).

## Setup

You need [uv](https://docs.astral.sh/uv/) and Docker (for Redis).

```bash
uv sync
cp .env.example .env              # then edit .env, see below
docker compose up -d redis        # Redis 8.2 on localhost:6379
uv run uvicorn app:app --app-dir src --reload --port 5999
```

Open http://localhost:5999/docs to try the API. To run the app in Docker as well,
use `docker compose up -d`. Settings are read at startup, so restart the server
after editing `.env`.

### Model provider
Pick one provider in `.env`:

| Provider | `.env` |
|----------|--------|
| Ollama (local, default) | `LLM_PROVIDER=ollama`, `LLM_MODEL=llama3.1`, `OLLAMA_BASE_URL=http://localhost:11434` |
| Anthropic | `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-sonnet-5-5`, `ANTHROPIC_API_KEY=...` |
| Google Gemini | `LLM_PROVIDER=google`, `LLM_MODEL=gemini-3.5-flash-lite`, `GOOGLE_API_KEY=...` |

For Ollama:
- If not yet pulled, pull the model first (`ollama pull <model_name>`). It must support tool calls.
- Local models can be slow. If runs time out, raise `RUN_TIMEOUT_SECONDS`
  (e.g. 300).
- When the app runs in Docker, `localhost` is the container, so set
  `OLLAMA_BASE_URL` to the host's address.

### Web search
`web_search` uses [Tavily](https://tavily.com). Set `TAVILY_API_KEY` in `.env`.
Without it the agent still runs, but every search returns an error to the model.

### Logging
`LOG_LEVEL` (`debug`, `info`, `warning`, `error`; default `info`) sets how much the
server logs. At `info` each run logs its start and end, every graph step and every
tool call, tagged with the thread id. Message text and answers are never logged;
`debug` adds tool inputs.

## Development

Managed with Astral UV:
```bash
uv sync
```

Static Analysis and formatting (run via uv):
```bash
make lint     # lint files with ruff
make fix      # lint files and fix
make format   # format files with ruff
make pytests  # Run pytest on tests
make trace    # check spec traceability, write docs/traceability.md
```
