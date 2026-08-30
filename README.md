# Eva AI

Eva is a proactive, event-driven personal AI operator. The repository contains the application foundation, durable Event backbone, and local Gmail ingestion worker.

## Requirements

- Python 3.14 (managed automatically by `uv`)
- uv 0.12+
- Docker with Docker Compose

## Local setup

Copy `.env.example` to `.env`, then run:

```bash
make setup
make db-up
make migrate
make run
```

The API listens on `http://127.0.0.1:8000`.

```bash
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

Both endpoints return HTTP 200 when the application and database are ready.

## Gmail ingestion

Milestone 2 adds Desktop OAuth bootstrap, Gmail watch/history synchronization, a Google Pub/Sub pull subscriber, persisted watch maintenance, and expired-cursor recovery. Setup requires manual Google Auth Platform configuration and local GCP resources; follow the [Gmail ingestion operator guide](docs/gmail-setup.md) before running the worker.

The local command surface is:

```bash
uv run eva scope create --display-name "Saswat Ray" --workspace-name personal
uv run eva gmail connect --user-id USER_UUID --workspace-id WORKSPACE_UUID
uv run eva gmail sync --connector-id CONNECTOR_UUID
uv run eva gmail pull
uv run eva gmail maintain
```

The four Gmail Make wrappers consume IDs from exported environment variables as literal shell
data. Create the scope with the direct CLI command above, export the UUIDs it prints, then use:

```bash
export EVA_USER_ID=USER_UUID
export EVA_WORKSPACE_ID=WORKSPACE_UUID
export EVA_GMAIL_CONNECTOR_ID=CONNECTOR_UUID
make gmail-connect
make gmail-sync
make gmail-pull
make gmail-maintain
```

There is no public Gmail webhook or HTTP ingestion endpoint. Gmail notifications are consumed from the configured pull subscription.

## Verification

With PostgreSQL running:

```bash
make verify
```

This runs Ruff formatting and lint checks, strict mypy checking, unit tests, PostgreSQL integration tests, and migration verification.

## Database migrations

Create revisions deliberately and review them before applying:

```bash
uv run alembic revision --autogenerate -m "describe change"
uv run alembic upgrade head
```

Migrations never run automatically during API startup.

## Architecture

The product architecture is in `spec/2026-08-29-proactive-personal-ai-agent-design.md`. Milestone designs and implementation plans are stored under `docs/superpowers/`.

### Milestone 1: Event backbone

Milestone 1 adds a durable event backbone with this flow:

```text
NewEvent -> PostgreSQL transaction [Event + EventProcessing + OutboxMessage]
         -> OutboxRelay claim -> Publisher acknowledgement
         -> EventProcessor claim -> EventHandler -> HANDLED
```

Local tests use the in-memory publisher. When the Google Pub/Sub adapter is selected, it
uses Application Default Credentials and requires `EVA_PUBSUB_PROJECT_ID`; this milestone
does not create any GCP resources. Publication is at-least-once, so event handling remains
idempotent across redelivery.

Milestone 1's Event and Outbox reliability boundary is reused by the Milestone 2 Gmail subscriber. Telegram behavior remains deferred.
