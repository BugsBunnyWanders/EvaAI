# Freeze operator inputs as literal values before exporting them to recipe shells.
override EVA_USER_ID := $(value EVA_USER_ID)
override EVA_WORKSPACE_ID := $(value EVA_WORKSPACE_ID)
override EVA_GMAIL_CONNECTOR_ID := $(value EVA_GMAIL_CONNECTOR_ID)
override EVA_EVENT_ID := $(value EVA_EVENT_ID)
override EVA_RELEVANCE_REASON := $(value EVA_RELEVANCE_REASON)
override EVA_RELEVANCE_IDEMPOTENCY_KEY := $(value EVA_RELEVANCE_IDEMPOTENCY_KEY)
override EVA_MEMORY_ID := $(value EVA_MEMORY_ID)
override EVA_MEMORY_QUERY := $(value EVA_MEMORY_QUERY)
override EVA_SITUATION_ID := $(value EVA_SITUATION_ID)
export EVA_USER_ID EVA_WORKSPACE_ID EVA_GMAIL_CONNECTOR_ID EVA_EVENT_ID EVA_RELEVANCE_REASON EVA_RELEVANCE_IDEMPOTENCY_KEY EVA_MEMORY_ID EVA_MEMORY_QUERY EVA_SITUATION_ID

.PHONY: setup db-up db-down migrate run worker-run gmail-connect gmail-sync gmail-pull gmail-maintain events-relay relevance-pull relevance-show relevance-history relevance-reevaluate relevance-backfill memory-fact-list memory-episode-list memory-episode-show memory-episode-search context-build test lint format typecheck verify

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

worker-run:
	uv run eva worker run

gmail-connect:
	uv run eva gmail connect --user-id "$${EVA_USER_ID}" --workspace-id "$${EVA_WORKSPACE_ID}"

gmail-sync:
	uv run eva gmail sync --connector-id "$${EVA_GMAIL_CONNECTOR_ID}"

gmail-pull:
	uv run eva gmail pull

gmail-maintain:
	uv run eva gmail maintain

events-relay:
	uv run eva events relay

relevance-pull:
	uv run eva relevance pull

relevance-show:
	uv run eva relevance show --user-id "$${EVA_USER_ID}" --workspace-id "$${EVA_WORKSPACE_ID}" --event-id "$${EVA_EVENT_ID}"

relevance-history:
	uv run eva relevance history --user-id "$${EVA_USER_ID}" --workspace-id "$${EVA_WORKSPACE_ID}" --event-id "$${EVA_EVENT_ID}"

relevance-reevaluate:
	uv run eva relevance reevaluate --user-id "$${EVA_USER_ID}" --workspace-id "$${EVA_WORKSPACE_ID}" --event-id "$${EVA_EVENT_ID}" --reason "$${EVA_RELEVANCE_REASON}" --idempotency-key "$${EVA_RELEVANCE_IDEMPOTENCY_KEY}"

relevance-backfill:
	uv run eva relevance backfill --user-id "$${EVA_USER_ID}" --workspace-id "$${EVA_WORKSPACE_ID}"

memory-fact-list:
	uv run eva memory fact list --user-id "$${EVA_USER_ID}" --workspace-id "$${EVA_WORKSPACE_ID}"

memory-episode-list:
	uv run eva memory episode list --user-id "$${EVA_USER_ID}" --workspace-id "$${EVA_WORKSPACE_ID}"

memory-episode-show:
	uv run eva memory episode show --user-id "$${EVA_USER_ID}" --workspace-id "$${EVA_WORKSPACE_ID}" --memory-id "$${EVA_MEMORY_ID}"

memory-episode-search:
	uv run eva memory episode search --user-id "$${EVA_USER_ID}" --workspace-id "$${EVA_WORKSPACE_ID}" --query "$${EVA_MEMORY_QUERY}"

context-build:
	uv run eva context build --user-id "$${EVA_USER_ID}" --workspace-id "$${EVA_WORKSPACE_ID}" --situation-id "$${EVA_SITUATION_ID}"

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
