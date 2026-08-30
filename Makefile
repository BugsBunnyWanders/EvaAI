.PHONY: setup db-up db-down migrate run scope-create gmail-connect gmail-sync gmail-pull gmail-maintain test lint format typecheck verify

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

scope-create:
	uv run eva scope create --display-name "$(EVA_DISPLAY_NAME)" --workspace-name "$(EVA_WORKSPACE_NAME)"

gmail-connect:
	uv run eva gmail connect --user-id "$(EVA_USER_ID)" --workspace-id "$(EVA_WORKSPACE_ID)"

gmail-sync:
	uv run eva gmail sync --connector-id "$(EVA_GMAIL_CONNECTOR_ID)"

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
