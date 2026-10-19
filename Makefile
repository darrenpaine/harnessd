# Helper to run commands with make

## Running in Docker
up:
	sudo docker compose up -d
redis:
	sudo docker compose up -d redis
langfuse:  # Langfuse on http://localhost:3000, for traces (SPEC-0010)
	sudo docker compose --profile observability up -d langfuse-web langfuse-worker
down:
	sudo docker compose --profile observability down
build:
	sudo docker compose build
logs:
	sudo docker compose logs harnessd

## Working locally
run:
	uv run uvicorn app:app --app-dir src --reload --port 5999
lint:
	uv run ruff check .
fix:
	uv run ruff check --fix .
format:
	uv run ruff format .
pytest:
	uv run pytest ./tests
coverage:
	uv run pytest --cov=src ./tests
mypy:
	uv run mypy src/ tests/ scripts/
openapi:
	uv run python scripts/export_openapi.py
trace:
	uv run python scripts/trace.py

## Git checks
pre-commit:
	uv run pre-commit run --all-files
pre-commit-setup:
	uv run pre-commit install
