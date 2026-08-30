# Eva AI

Eva is a proactive, event-driven personal AI operator. The repository currently contains the Milestone 0 application foundation.

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

Milestone 1 provides one-pass composition helpers only. It does not add a subscriber,
long-running worker loop, or public endpoint. Telegram and Gmail behavior are deferred to
later milestones.
