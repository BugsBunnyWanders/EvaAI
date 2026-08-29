# Milestone 0 Foundation Design

**Status:** Approved for implementation
**Date:** 2026-08-30
**Product specification:** `spec/2026-08-29-proactive-personal-ai-agent-design.md`

## Objective

Establish a small, production-oriented Python application foundation for Eva. The milestone must provide a runnable API, typed configuration, PostgreSQL connectivity, schema migrations, structured logging, automated tests, and repeatable local development without implementing product-domain behavior from later milestones.

## Decisions

- The distribution/project name is `eva-ai`.
- The import package is `eva_ai` under `src/eva_ai`.
- Python is pinned to 3.14.
- PostgreSQL is pinned to major version 17.
- Local PostgreSQL runs with pgvector through Docker Compose.
- Deployed PostgreSQL will use Cloud SQL with pgvector. Local and deployed environments use the same SQLAlchemy models and Alembic migrations.
- Eva runs locally through `uv`; it does not run inside Docker during this milestone.
- The milestone establishes one modular application shell rather than separate deployable API and worker services. Later milestones may create distinct entry points from the shared package.

## Scope

Milestone 0 includes:

- `uv` project metadata and a committed lockfile
- a FastAPI application factory
- liveness and database-backed readiness endpoints
- typed environment configuration using Pydantic Settings
- async SQLAlchemy 2 engine and session management using Psycopg 3
- Alembic configuration and an initial migration enabling pgvector
- PostgreSQL 17 with pgvector in Docker Compose
- structured application logging
- unit and PostgreSQL integration tests
- Ruff formatting and linting
- strict mypy type checking
- GitHub Actions verification on pushes and pull requests
- local development and verification instructions

Milestone 0 does not include:

- Event, Signal, Situation, Goal, ActionProposal, or Action models
- users, workspaces, connectors, memory, or conversation tables
- Gmail, Telegram, Calendar, or other integrations
- an LLM provider or agent runtime
- Pub/Sub, Cloud Tasks, or an outbox
- Terraform or deployed GCP resources
- application-level retry or workflow behavior

## Application Structure

The package uses an application factory so importing modules does not create infrastructure or open network connections.

```text
src/eva_ai/
  __init__.py
  main.py                 FastAPI application factory and ASGI app
  config.py               Typed environment settings
  logging.py              Local and JSON logging configuration
  api/
    __init__.py
    health.py             Liveness and readiness routes
  db/
    __init__.py
    base.py               SQLAlchemy declarative base
    session.py            Async engine/session lifecycle and dependency
```

The dependency direction is:

```text
FastAPI routes
    -> application dependencies
        -> configuration / database session
            -> PostgreSQL

Alembic -> PostgreSQL schema
```

Routes do not construct engines or sessions. Database plumbing remains behind `eva_ai.db`. Domain modules will be introduced only when their milestones require them.

## Configuration

Settings are read from environment variables, with local `.env` loading supported for developer convenience. `.env` is ignored by Git, while `.env.example` documents non-secret local defaults.

The foundation settings cover:

- application name and environment
- log level and log format
- database URL

Configuration is validated at startup. Invalid configuration fails fast with a clear error. Credentials and complete database URLs must not appear in logs or HTTP responses. Production secret delivery through Secret Manager is deferred until deployment work; the application boundary accepts injected environment configuration now.

## Database and Migrations

SQLAlchemy uses an async engine and `AsyncSession` with Psycopg 3. Engine creation is lazy and controlled by application lifecycle code. Application shutdown disposes the engine cleanly.

Alembic is the only schema migration mechanism. Migrations never run implicitly when the API starts. The initial migration enables the `vector` extension and intentionally creates no product tables.

Docker Compose runs a pgvector-enabled PostgreSQL 17 image with:

- a persistent named volume
- a container health check
- local-only bootstrap credentials documented in `.env.example`

The Cloud SQL transition changes the database URL and credential delivery, not application or migration code.

## HTTP Health Contract

`GET /health/live` reports whether the HTTP process is responsive. It has no external dependency and returns HTTP 200 with a stable machine-readable body.

`GET /health/ready` executes a lightweight database query. It returns HTTP 200 when PostgreSQL is reachable and HTTP 503 when the query fails. Failure details are logged for operators but the response does not expose database URLs, credentials, SQL internals, or stack traces.

## Logging and Error Handling

The application uses Python standard logging with centralized configuration:

- readable console logs in local development
- structured JSON logs in deployed environments
- consistent timestamp, severity, logger, and message fields

Unrecoverable configuration errors fail startup. A database outage does not make the liveness endpoint fail; it makes readiness return 503. This distinction lets an orchestrator separate a dead process from a temporarily unavailable dependency.

## Testing and Verification

Tests are split by dependency level:

```text
tests/unit/          No external services
tests/integration/   Real PostgreSQL 17 with pgvector
```

Required behavior includes:

- environment settings parse and validate correctly
- liveness returns the stable success contract
- readiness returns success when the database query succeeds
- readiness returns a safe 503 contract when the database query fails
- async sessions can execute against PostgreSQL
- Alembic upgrades a clean database successfully
- the pgvector extension exists after migration

The milestone verification gate runs:

1. Ruff formatting check
2. Ruff lint
3. strict mypy type checking
4. unit tests
5. PostgreSQL integration tests
6. Alembic upgrade verification
7. application import/startup check

GitHub Actions runs the same checks for pushes and pull requests using a pgvector-enabled PostgreSQL service. No arbitrary coverage percentage is imposed; every introduced behavior must have a direct test.

## Completion Criteria

Milestone 0 is complete when a new developer can:

1. install the pinned Python environment and dependencies with `uv`;
2. start PostgreSQL with Docker Compose;
3. apply Alembic migrations to a clean database;
4. run Eva locally;
5. receive HTTP 200 from liveness and readiness;
6. run the complete local verification suite successfully; and
7. observe the same verification suite succeed in GitHub Actions.

On completion, all milestone changes are committed and pushed to the remote repository with the verification evidence and commit hash reported.
