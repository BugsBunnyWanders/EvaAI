.PHONY: setup db-up db-down migrate run test lint format typecheck verify

setup:
	uv sync --all-groups

db-up:
	docker compose up -d --wait postgres

db-down:
	docker compose down

migrate:
	uv run alembic upgrade head

run:
	uv run uvicorn eva_ai.main:app --reload

test:
	uv run pytest -v

lint:
	uv run ruff format --check src migrations tests
	uv run ruff check .

format:
	uv run ruff format src migrations tests
	uv run ruff check --fix .

typecheck:
	uv run mypy src migrations tests

verify: lint typecheck test
