# Freeze operator inputs as literal values before exporting them to recipe shells.
override EVA_USER_ID := $(value EVA_USER_ID)
override EVA_WORKSPACE_ID := $(value EVA_WORKSPACE_ID)
override EVA_GMAIL_CONNECTOR_ID := $(value EVA_GMAIL_CONNECTOR_ID)
export EVA_USER_ID EVA_WORKSPACE_ID EVA_GMAIL_CONNECTOR_ID

.PHONY: setup db-up db-down migrate run gmail-connect gmail-sync gmail-pull gmail-maintain test lint format typecheck verify

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

gmail-connect:
	uv run eva gmail connect --user-id "$${EVA_USER_ID}" --workspace-id "$${EVA_WORKSPACE_ID}"

gmail-sync:
	uv run eva gmail sync --connector-id "$${EVA_GMAIL_CONNECTOR_ID}"

gmail-pull:
	uv run eva gmail pull

gmail-maintain:
	uv run eva gmail maintain

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
