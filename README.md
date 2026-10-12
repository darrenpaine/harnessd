# harnessd

A base for AI agent usage, with an example Python/FastAPI code project.

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