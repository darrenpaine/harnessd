# Helper to run commands with make

## Running in Docker
up:
	sudo docker compose up -d
down:
	sudo docker compose down
build:
	sudo docker compose build
logs:
	sudo docker compose logs harnessd

## Working locally
run:
	uv run uvicorn src.harnessd.app:app --reload --port 5999
lint:
	uv run ruff check .
fix:
	uv run ruff check --fix .
format:
	uv run ruff format .
pytest:
	uv run pytest ./tests
mypy:
	uv run mypy .
