# Helper to run commands with make

lint:
	uv run ruff check .

fix:
	uv run ruff check --fix .

format:
	uv run ruff format .

pytest:
	uv run pytest ./tests

run:
	uv run uvicorn src.harnessd.app:app --reload --port 5999

mypy:
	uv run mypy .
