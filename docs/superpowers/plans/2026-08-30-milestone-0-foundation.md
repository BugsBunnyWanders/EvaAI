# Milestone 0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Eva's runnable, tested Python application foundation with FastAPI, typed configuration, PostgreSQL 17 plus pgvector, Alembic, structured logging, and continuous verification.

**Architecture:** A `src/eva_ai` modular application package exposes a FastAPI application factory, centralized settings and logging, and a small async database boundary. PostgreSQL runs locally in Docker Compose and in Cloud SQL later; Alembic is the sole schema migration mechanism and health endpoints distinguish process liveness from database readiness.

**Tech Stack:** Python 3.14, uv 0.12.x, FastAPI, Pydantic Settings, SQLAlchemy 2, Psycopg 3, Alembic, PostgreSQL 17, pgvector, pytest, Ruff, mypy, Docker Compose, GitHub Actions

**Spec:** `docs/superpowers/specs/2026-08-30-milestone-0-foundation-design.md`

## Global Constraints

- Distribution name: `eva-ai`; import package: `eva_ai`.
- Python requirement: `>=3.14,<3.15`; `.python-version` contains `3.14`.
- PostgreSQL major version: 17, using a pgvector-enabled image locally.
- Run Eva locally through `uv`; only PostgreSQL runs in Docker for Milestone 0.
- Local and Cloud SQL environments use the same SQLAlchemy and Alembic code.
- Do not add product-domain models, external integrations, agent code, GCP infrastructure, or asynchronous event infrastructure.
- Do not open network connections during module import.
- Never log or return database credentials or complete database URLs.
- Every application behavior follows red-green-refactor; configuration-only files may be created before the first test runner invocation.
- Do not push intermediate commits. Push the complete, verified milestone to `origin` only after all tasks pass.

## File Map

```text
.env.example                         Safe local environment template
.github/workflows/ci.yml             Push and pull-request verification
.gitignore                           Local, Python, IDE, secret, and macOS ignores
.python-version                      uv Python selection
Makefile                             Stable local developer commands
README.md                            Setup, run, migrate, test, and architecture notes
alembic.ini                          Alembic command configuration without credentials
compose.yaml                         PostgreSQL 17 + pgvector local service
pyproject.toml                       Package, dependencies, and tool configuration
uv.lock                              Reproducible dependency resolution
migrations/env.py                    Async Alembic environment using Eva settings
migrations/script.py.mako            Alembic revision template
migrations/versions/...py            Initial pgvector migration
src/eva_ai/__init__.py               Package version
src/eva_ai/config.py                 Typed environment settings
src/eva_ai/logging.py                Console and JSON logging configuration
src/eva_ai/main.py                   Application factory, lifespan, and ASGI app
src/eva_ai/api/__init__.py           API package
src/eva_ai/api/dependencies.py       Request-scoped infrastructure lookup
src/eva_ai/api/health.py             Liveness and readiness contracts
src/eva_ai/db/__init__.py            Public database exports
src/eva_ai/db/base.py                SQLAlchemy declarative base
src/eva_ai/db/session.py             Async engine/session lifecycle
tests/unit/test_config.py            Settings behavior
tests/unit/test_logging.py           Logging behavior
tests/unit/api/test_health.py        Health API behavior
tests/integration/conftest.py         Migration setup for PostgreSQL tests
tests/integration/test_database.py   Real database boundary behavior
tests/integration/test_migrations.py pgvector migration behavior
```

---

### Task 1: Project metadata and typed configuration

**Files:**
- Create: `.gitignore`
- Create: `.python-version`
- Create: `.env.example`
- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `src/eva_ai/__init__.py`
- Create: `src/eva_ai/config.py`
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: environment variables with the `EVA_` prefix.
- Produces: `AppEnvironment`, `LogFormat`, `Settings`, and cached `get_settings() -> Settings`.

- [ ] **Step 1: Add configuration-only project scaffolding**

Create `.python-version`:

```text
3.14
```

Create `.gitignore`:

```gitignore
.DS_Store
.env
.venv/
.mypy_cache/
.pytest_cache/
.ruff_cache/
__pycache__/
*.py[cod]
*.egg-info/
.coverage
htmlcov/
```

Create `.env.example`:

```dotenv
EVA_APP_NAME=Eva
EVA_ENVIRONMENT=local
EVA_LOG_LEVEL=INFO
EVA_LOG_FORMAT=console
EVA_DATABASE_URL=postgresql+psycopg://eva:eva@localhost:5432/eva

POSTGRES_USER=eva
POSTGRES_PASSWORD=eva
POSTGRES_DB=eva
POSTGRES_PORT=5432
```

Create `pyproject.toml`:

```toml
[project]
name = "eva-ai"
version = "0.1.0"
description = "A proactive personal AI operator"
readme = "README.md"
requires-python = ">=3.14,<3.15"
dependencies = [
    "alembic>=1.16",
    "fastapi>=0.116",
    "psycopg[binary,pool]>=3.2",
    "pydantic-settings>=2.10",
    "sqlalchemy[asyncio]>=2.0",
    "uvicorn[standard]>=0.35",
]

[dependency-groups]
dev = [
    "httpx>=0.28",
    "mypy>=1.17",
    "pytest>=8.4",
    "pytest-asyncio>=1.1",
    "ruff>=0.12",
]

[build-system]
requires = ["uv_build>=0.12.5,<0.13"]
build-backend = "uv_build"

[tool.pytest.ini_options]
addopts = "-ra --strict-config --strict-markers"
asyncio_mode = "auto"
testpaths = ["tests"]
markers = ["integration: requires PostgreSQL"]

[tool.ruff]
target-version = "py314"
line-length = 100

[tool.ruff.lint]
select = ["ASYNC", "B", "E", "F", "I", "UP"]

[tool.mypy]
python_version = "3.14"
strict = true
plugins = ["pydantic.mypy"]
packages = ["eva_ai"]

[[tool.mypy.overrides]]
module = "tests.*"
disallow_untyped_defs = true
```

Create package/test `__init__.py` files. In `src/eva_ai/__init__.py`, expose:

```python
__version__ = "0.1.0"
```

Run:

```bash
uv lock
uv sync --all-groups
```

Expected: `uv.lock` and `.venv` are created using Python 3.14; dependency resolution succeeds.

- [ ] **Step 2: Write failing settings tests**

Create `tests/unit/test_config.py`:

```python
import pytest
from pydantic import SecretStr, ValidationError

from eva_ai.config import AppEnvironment, LogFormat, Settings


def test_settings_have_safe_local_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "Eva"
    assert settings.environment is AppEnvironment.LOCAL
    assert settings.log_level == "INFO"
    assert settings.log_format is LogFormat.CONSOLE
    assert isinstance(settings.database_url, SecretStr)
    assert "eva:eva" not in str(settings.database_url)


def test_settings_read_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVA_ENVIRONMENT", "production")
    monkeypatch.setenv("EVA_LOG_FORMAT", "json")
    monkeypatch.setenv("EVA_LOG_LEVEL", "warning")

    settings = Settings(_env_file=None)

    assert settings.environment is AppEnvironment.PRODUCTION
    assert settings.log_format is LogFormat.JSON
    assert settings.log_level == "WARNING"


def test_settings_reject_unknown_log_level() -> None:
    with pytest.raises(ValidationError):
        Settings(log_level="VERBOSE", _env_file=None)
```

- [ ] **Step 3: Run settings tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_config.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'eva_ai.config'`.

- [ ] **Step 4: Implement typed settings**

Create `src/eva_ai/config.py`:

```python
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    CONSOLE = "console"
    JSON = "json"


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="EVA_",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Eva"
    environment: AppEnvironment = AppEnvironment.LOCAL
    log_level: LogLevel = "INFO"
    log_format: LogFormat = LogFormat.CONSOLE
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://eva:eva@localhost:5432/eva"
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 5: Run settings tests and quality checks; verify GREEN**

Run:

```bash
uv run pytest tests/unit/test_config.py -v
uv run ruff check src/eva_ai/config.py tests/unit/test_config.py
uv run mypy src/eva_ai/config.py
```

Expected: 3 tests pass; Ruff and mypy exit 0.

- [ ] **Step 6: Commit Task 1**

```bash
git add .gitignore .python-version .env.example pyproject.toml uv.lock src/eva_ai tests
git commit -m "build: bootstrap Eva Python project"
```

---

### Task 2: Structured logging

**Files:**
- Create: `src/eva_ai/logging.py`
- Create: `tests/unit/test_logging.py`

**Interfaces:**
- Consumes: `Settings.log_level` and `Settings.log_format`.
- Produces: `JsonFormatter`, `configure_logging(settings: Settings, stream: TextIO | None = None) -> None`.

- [ ] **Step 1: Write failing logging tests**

Create `tests/unit/test_logging.py`:

```python
import io
import json
import logging

from eva_ai.config import LogFormat, Settings
from eva_ai.logging import JsonFormatter, configure_logging


def test_json_formatter_emits_cloud_friendly_fields() -> None:
    record = logging.LogRecord(
        name="eva.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="service ready",
        args=(),
        exc_info=None,
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["severity"] == "INFO"
    assert payload["logger"] == "eva.test"
    assert payload["message"] == "service ready"
    assert payload["timestamp"].endswith("Z")


def test_configure_logging_replaces_root_handlers() -> None:
    stream = io.StringIO()
    settings = Settings(log_level="WARNING", log_format=LogFormat.JSON, _env_file=None)

    configure_logging(settings, stream=stream)
    logging.getLogger("eva.test").warning("careful")

    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert len(root.handlers) == 1
    assert json.loads(stream.getvalue())["message"] == "careful"
```

- [ ] **Step 2: Run logging tests and verify RED**

Run:

```bash
uv run pytest tests/unit/test_logging.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'eva_ai.logging'`.

- [ ] **Step 3: Implement centralized logging**

Create `src/eva_ai/logging.py`:

```python
import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

from eva_ai.config import LogFormat, Settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(settings: Settings, stream: TextIO | None = None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    if settings.log_format is LogFormat.JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)
```

- [ ] **Step 4: Run logging tests and checks; verify GREEN**

Run:

```bash
uv run pytest tests/unit/test_logging.py -v
uv run ruff check src/eva_ai/logging.py tests/unit/test_logging.py
uv run mypy src/eva_ai/logging.py
```

Expected: 2 tests pass; Ruff and mypy exit 0.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/eva_ai/logging.py tests/unit/test_logging.py
git commit -m "feat: add structured application logging"
```

---

### Task 3: Async database runtime and local PostgreSQL

**Files:**
- Create: `compose.yaml`
- Create: `src/eva_ai/db/__init__.py`
- Create: `src/eva_ai/db/base.py`
- Create: `src/eva_ai/db/session.py`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_database.py`

**Interfaces:**
- Consumes: a SQLAlchemy database URL string from `Settings.database_url.get_secret_value()`.
- Produces: `Base`, `Database(database_url: str)`, `Database.ping()`, `Database.session()`, and `Database.close()`.

- [ ] **Step 1: Add the local PostgreSQL service configuration**

Create `compose.yaml`:

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-eva}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-eva}
      POSTGRES_DB: ${POSTGRES_DB:-eva}
    ports:
      - "${POSTGRES_PORT:-5432}:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-eva} -d ${POSTGRES_DB:-eva}"]
      interval: 2s
      timeout: 5s
      retries: 10
    volumes:
      - eva-postgres-data:/var/lib/postgresql/data

volumes:
  eva-postgres-data:
```

Run:

```bash
docker compose config --quiet
docker compose up -d --wait postgres
```

Expected: Compose configuration validates and `postgres` becomes healthy.

- [ ] **Step 2: Write the failing database integration test**

Create `tests/integration/test_database.py`:

```python
import pytest
from sqlalchemy import text

from eva_ai.config import Settings
from eva_ai.db.session import Database


@pytest.mark.integration
async def test_database_can_ping_and_open_session() -> None:
    settings = Settings(_env_file=None)
    database = Database(settings.database_url.get_secret_value())

    try:
        await database.ping()
        async with database.session() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    finally:
        await database.close()
```

- [ ] **Step 3: Run the database test and verify RED**

Run:

```bash
uv run pytest tests/integration/test_database.py -v -m integration
```

Expected: collection fails with `ModuleNotFoundError: No module named 'eva_ai.db'`.

- [ ] **Step 4: Implement the database boundary**

Create `src/eva_ai/db/base.py`:

```python
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
```

Create `src/eva_ai/db/session.py`:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class Database:
    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self._session_factory = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            yield session

    async def ping(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def close(self) -> None:
        await self._engine.dispose()
```

Create `src/eva_ai/db/__init__.py`:

```python
from eva_ai.db.base import Base
from eva_ai.db.session import Database

__all__ = ["Base", "Database"]
```

- [ ] **Step 5: Run the integration test; verify GREEN**

Run:

```bash
uv run pytest tests/integration/test_database.py -v -m integration
uv run ruff check src/eva_ai/db tests/integration/test_database.py
uv run mypy src/eva_ai/db
```

Expected: 1 integration test passes; Ruff and mypy exit 0.

- [ ] **Step 6: Commit Task 3**

```bash
git add compose.yaml src/eva_ai/db tests/integration
git commit -m "feat: add async PostgreSQL runtime"
```

---

### Task 4: FastAPI lifecycle and health endpoints

**Files:**
- Create: `src/eva_ai/api/__init__.py`
- Create: `src/eva_ai/api/dependencies.py`
- Create: `src/eva_ai/api/health.py`
- Create: `src/eva_ai/main.py`
- Create: `tests/unit/api/__init__.py`
- Create: `tests/unit/api/test_health.py`

**Interfaces:**
- Consumes: `Settings`, `configure_logging()`, and `Database`.
- Produces: `create_app(settings: Settings | None = None) -> FastAPI`, ASGI `app`, `get_database(request: Request) -> Database`, `/health/live`, and `/health/ready`.

- [ ] **Step 1: Write failing health API tests**

Create `tests/unit/api/test_health.py`:

```python
from fastapi.testclient import TestClient

from eva_ai.api.dependencies import get_database
from eva_ai.config import Settings
from eva_ai.main import create_app


class PassingProbe:
    async def ping(self) -> None:
        return None


class FailingProbe:
    async def ping(self) -> None:
        raise RuntimeError("postgresql+psycopg://secret:secret@database/eva")


def test_liveness_does_not_require_database() -> None:
    application = create_app(Settings(_env_file=None))

    with TestClient(application) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_ready_when_database_responds() -> None:
    application = create_app(Settings(_env_file=None))
    application.dependency_overrides[get_database] = PassingProbe

    with TestClient(application) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_returns_safe_failure_body() -> None:
    application = create_app(Settings(_env_file=None))
    application.dependency_overrides[get_database] = FailingProbe

    with TestClient(application) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "secret" not in response.text
```

- [ ] **Step 2: Run health tests and verify RED**

Run:

```bash
uv run pytest tests/unit/api/test_health.py -v
```

Expected: collection fails because `eva_ai.api.dependencies` and `eva_ai.main` do not exist.

- [ ] **Step 3: Implement request dependencies and health routes**

Create `src/eva_ai/api/dependencies.py`:

```python
from typing import cast

from fastapi import Request

from eva_ai.db import Database


def get_database(request: Request) -> Database:
    return cast(Database, request.app.state.database)
```

Create `src/eva_ai/api/health.py`:

```python
import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from eva_ai.api.dependencies import get_database
from eva_ai.db import Database

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def readiness(
    database: Annotated[Database, Depends(get_database)],
) -> dict[str, str] | JSONResponse:
    try:
        await database.ping()
    except Exception as error:
        logger.warning("Database readiness check failed: %s", type(error).__name__)
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return {"status": "ready"}
```

The broad exception boundary is deliberate at the readiness edge: any failed dependency probe means not ready, while the response remains secret-free.

- [ ] **Step 4: Implement application factory and lifespan**

Create `src/eva_ai/main.py`:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from eva_ai.api.health import router as health_router
from eva_ai.config import Settings, get_settings
from eva_ai.db import Database
from eva_ai.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved_settings.database_url.get_secret_value())
        application.state.database = database
        try:
            yield
        finally:
            await database.close()

    application = FastAPI(title=resolved_settings.app_name, lifespan=lifespan)
    application.state.settings = resolved_settings
    application.include_router(health_router)
    return application


app = create_app()
```

Create empty `src/eva_ai/api/__init__.py` and `tests/unit/api/__init__.py`.

- [ ] **Step 5: Run health tests and checks; verify GREEN**

Run:

```bash
uv run pytest tests/unit/api/test_health.py -v
uv run ruff check src/eva_ai/api src/eva_ai/main.py tests/unit/api
uv run mypy src/eva_ai/api src/eva_ai/main.py
uv run python -c 'from eva_ai.main import app; assert app.title == "Eva"'
```

Expected: 3 tests pass; Ruff, mypy, and the import check exit 0.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/eva_ai/api src/eva_ai/main.py tests/unit/api
git commit -m "feat: add API lifecycle and health checks"
```

---

### Task 5: Alembic and pgvector migration

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/20260830_0001_enable_pgvector.py`
- Create: `tests/integration/conftest.py`
- Create: `tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: `Settings.database_url`, `Base.metadata`, and a clean PostgreSQL database.
- Produces: Alembic revision `20260830_0001`, schema head state, and installed `vector` extension.

- [ ] **Step 1: Create Alembic command configuration and async environment**

Create `alembic.ini` without a database URL:

```ini
[alembic]
script_location = migrations
prepend_sys_path = .

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

Create `migrations/env.py`:

```python
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from eva_ai.config import get_settings
from eva_ai.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    return get_settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

Create `migrations/script.py.mako`:

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 2: Write the failing migration test**

Create `tests/integration/conftest.py`:

```python
import pytest
from alembic import command
from alembic.config import Config


@pytest.fixture(scope="session", autouse=True)
def apply_migrations() -> None:
    command.upgrade(Config("alembic.ini"), "head")
```

Create `tests/integration/test_migrations.py`:

```python
import pytest
from sqlalchemy import text

from eva_ai.config import Settings
from eva_ai.db import Database


@pytest.mark.integration
async def test_initial_migration_installs_pgvector() -> None:
    settings = Settings(_env_file=None)
    database = Database(settings.database_url.get_secret_value())

    try:
        async with database.session() as session:
            extension = await session.scalar(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            )
    finally:
        await database.close()

    assert extension == "vector"
```

- [ ] **Step 3: Run migration test and verify RED**

Run:

```bash
uv run pytest tests/integration/test_migrations.py -v -m integration
```

Expected: Alembic fails because no revision installs the `vector` extension, or the final assertion reports `None`.

- [ ] **Step 4: Add the initial pgvector revision**

Create `migrations/versions/20260830_0001_enable_pgvector.py`:

```python
"""Enable pgvector.

Revision ID: 20260830_0001
Revises:
Create Date: 2026-08-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260830_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")
```

- [ ] **Step 5: Recreate the local database and verify GREEN from a clean state**

Because this is an empty Milestone 0 development database, reset only the explicitly named Compose volume, then recreate it:

```bash
docker compose down --volumes
docker compose up -d --wait postgres
uv run alembic upgrade head
uv run pytest tests/integration -v -m integration
uv run alembic current
```

Expected: migration reaches `20260830_0001 (head)`; both integration tests pass; pgvector is installed.

- [ ] **Step 6: Run migration quality checks**

```bash
uv run ruff check migrations tests/integration
uv run mypy migrations src/eva_ai/db
```

Expected: both commands exit 0.

- [ ] **Step 7: Commit Task 5**

```bash
git add alembic.ini migrations tests/integration
git commit -m "feat: initialize Alembic with pgvector"
```

---

### Task 6: Developer commands, documentation, and CI

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `Makefile`
- Modify: `README.md`

**Interfaces:**
- Consumes: all earlier project commands and the committed `uv.lock`.
- Produces: `make setup`, `make db-up`, `make migrate`, `make run`, `make test`, `make lint`, `make typecheck`, `make verify`, and equivalent CI checks.

- [ ] **Step 1: Add stable local commands**

Create `Makefile`:

```makefile
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
```

- [ ] **Step 2: Replace the placeholder README with exact developer instructions**

Update `README.md` to include:

```markdown
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
```

- [ ] **Step 3: Add GitHub Actions verification**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
  pull_request:

permissions:
  contents: read

jobs:
  verify:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg17
        env:
          POSTGRES_USER: eva
          POSTGRES_PASSWORD: eva
          POSTGRES_DB: eva
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U eva -d eva"
          --health-interval 2s
          --health-timeout 5s
          --health-retries 10
    env:
      EVA_ENVIRONMENT: test
      EVA_LOG_FORMAT: json
      EVA_DATABASE_URL: postgresql+psycopg://eva:eva@localhost:5432/eva
    steps:
      - name: Check out repository
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Install uv
        uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
        with:
          version: "0.12.5"
          enable-cache: true

      - name: Install Python
        run: uv python install

      - name: Sync dependencies
        run: uv sync --locked --all-groups

      - name: Check formatting
        run: uv run ruff format --check src migrations tests

      - name: Lint
        run: uv run ruff check .

      - name: Type check
        run: uv run mypy src migrations tests

      - name: Test
        run: uv run pytest -v
```

- [ ] **Step 4: Run the complete milestone verification gate**

Run from the repository root with the Compose database healthy:

```bash
uv lock --check
uv sync --locked --all-groups
docker compose config --quiet
docker compose up -d --wait postgres
uv run alembic upgrade head
uv run ruff format --check src migrations tests
uv run ruff check .
uv run mypy src migrations tests
uv run pytest -v
uv run alembic current
uv run python -c 'from eva_ai.main import app; assert app.title == "Eva"'
```

Expected:

- lockfile is current and sync succeeds;
- Compose validates and PostgreSQL becomes healthy;
- Alembic reports revision `20260830_0001 (head)`;
- Ruff formatting and linting exit 0;
- mypy exits 0 in strict mode;
- all unit and integration tests pass;
- the ASGI app imports without opening a database connection.

- [ ] **Step 5: Perform a live HTTP smoke test**

Start the API in one terminal:

```bash
uv run uvicorn eva_ai.main:app --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
curl --fail --silent http://127.0.0.1:8000/health/live
curl --fail --silent http://127.0.0.1:8000/health/ready
```

Expected bodies:

```json
{"status":"ok"}
{"status":"ready"}
```

Stop Uvicorn with Ctrl-C after the smoke test.

- [ ] **Step 6: Review requirements and repository diff**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -8
```

Confirm every completion criterion in the design document has evidence, `.DS_Store` and `.env` are absent from Git, and no later-milestone modules or secrets were introduced.

- [ ] **Step 7: Commit the final Milestone 0 automation and documentation**

```bash
git add .github/workflows/ci.yml Makefile README.md
git commit -m "ci: verify Eva foundation"
```

- [ ] **Step 8: Re-run verification against the committed tree and push**

Repeat Step 4 after the final commit. If every command exits 0:

```bash
git push origin HEAD
git status --short --branch
```

Expected: the push succeeds; the branch is aligned with `origin`; only ignored local files may remain.

## Plan Self-Review Results

- **Spec coverage:** All approved scope items map to Tasks 1–6. All explicit non-goals remain absent.
- **Type consistency:** `Settings`, `Database`, `get_database`, `create_app`, and health response contracts use the same names and signatures in their producer and consumer tasks.
- **Security:** Database URLs are `SecretStr`, are absent from Alembic configuration, and are never returned from health routes.
- **Reliability:** Liveness is dependency-free, readiness probes PostgreSQL, migrations are explicit, and clean-database migration behavior is exercised.
- **Reproducibility:** Python, PostgreSQL, uv in CI, action revisions, and dependency resolution are pinned or locked.
