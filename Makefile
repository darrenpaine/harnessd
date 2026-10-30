# Helper to run commands with make

## Running in Docker
up:
	sudo docker compose up -d
redis:
	sudo docker compose up -d redis
langfuse:  # Langfuse on http://localhost:5998, for traces (SPEC-0010)
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
evals:  # needs Redis; ARGS="--tag confirmation --repeats 1" for a partial run (SPEC-0011)
	uv run python scripts/run_evals.py $(ARGS)
evals-baseline:  # accept the configured model's latest full run as its baseline
	uv run python scripts/run_evals.py --accept-baseline
evals-report:  # regenerate evals/REPORT.md from the run files, without a model
	uv run python scripts/run_evals.py --report

## Git checks
pre-commit:
	uv run pre-commit run --all-files
pre-commit-setup:
	uv run pre-commit install
