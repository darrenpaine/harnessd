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
```
