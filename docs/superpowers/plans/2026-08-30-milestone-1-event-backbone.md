# Milestone 1 Event Backbone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Eva's durable, workspace-scoped Event backbone from atomic ingestion through at-least-once publication and idempotent worker dispatch.

**Architecture:** A SQLAlchemy application service persists an immutable Event, its EventProcessing record, and its OutboxMessage in one PostgreSQL transaction. Separate lease-based relay and processor services claim durable work in short transactions, perform network or handler work outside database locks, and complete only claims they still own. A typed Publisher protocol keeps local tests and the Google Pub/Sub adapter interchangeable.

**Tech Stack:** Python 3.14, Pydantic 2, FastAPI settings, SQLAlchemy 2 async ORM, PostgreSQL 17 with JSONB/ARRAY/pgvector extension, Alembic, `google-cloud-pubsub` 2.x, pytest/pytest-asyncio, Ruff, strict mypy, Docker Compose, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-30-milestone-1-event-backbone-design.md`

## Global Constraints

- Work on `codex/milestone-1-event-backbone`; push that branch and open a pull request to `main` when verification passes.
- Use the package import path `eva_ai` and the distribution name `eva-ai`.
- Support Python `>=3.14,<3.15` and PostgreSQL 17.
- External events are factual input only; this milestone must not authorize or execute user-facing actions.
- Preserve the durable flow `Event -> EventProcessing + OutboxMessage -> Publisher -> EventProcessor`.
- Enforce workspace-scoped idempotency with the exact unique key `(workspace_id, idempotency_key)`.
- Enforce `(workspace_id, user_id)` ownership with PostgreSQL constraints; never trust message scope alone.
- Hold database transactions only while reading or writing claims. Publish and invoke handlers after the claim transaction commits.
- Preserve at-least-once semantics. Never claim exactly-once publication or processing.
- Store only the exception class and the fixed safe summary `operation failed`; never persist exception messages, URLs, credentials, tokens, stack traces, or payload bodies as errors.
- Use Google Application Default Credentials; do not add credential values to Settings or source control.
- Add comments only where they explain invariants, transaction boundaries, leases, stale-claim protection, or safety behavior.
- Use test-driven development: observe every focused test fail before adding its production implementation.
- Do not add Gmail ingestion, public worker endpoints, long-running subscriber loops, GCP resource creation, Terraform, retry backoff, dead-letter queues, replay APIs, or administrative UI.

---

## File Map

### New production files

- `src/eva_ai/events/__init__.py` — public Event-backbone exports.
- `src/eva_ai/events/types.py` — canonical command, envelope, outbound message, enums, and validation.
- `src/eva_ai/events/errors.py` — typed backbone errors and secret-safe stored error summaries.
- `src/eva_ai/events/service.py` — atomic Event ingestion and duplicate resolution.
- `src/eva_ai/events/publisher.py` — Publisher protocol and deterministic in-memory publisher.
- `src/eva_ai/events/outbox.py` — lease-based outbox claiming, publication, completion, and release.
- `src/eva_ai/events/processor.py` — handler protocol and lease-based idempotent Event dispatch.
- `src/eva_ai/db/models/__init__.py` — imports all ORM models into `Base.metadata`.
- `src/eva_ai/db/models/common.py` — UUID and timestamp mixins.
- `src/eva_ai/db/models/identity.py` — minimal User and Workspace models.
- `src/eva_ai/db/models/events.py` — Event, EventProcessing, and OutboxMessage models.
- `src/eva_ai/integrations/__init__.py` — integration package marker.
- `src/eva_ai/integrations/gcp/__init__.py` — GCP integration package marker.
- `src/eva_ai/integrations/gcp/pubsub.py` — Google Pub/Sub publisher adapter.
- `src/eva_ai/worker.py` — dependency composition and one-message dispatch entry points; no loop.
- `migrations/versions/20260830_0002_event_backbone.py` — Milestone 1 schema and constraints.

### New test files

- `tests/unit/events/test_types.py` — command/envelope validation and serialization.
- `tests/unit/events/test_errors.py` — stored error safety.
- `tests/unit/events/test_publisher.py` — in-memory publisher contract.
- `tests/unit/integrations/gcp/test_pubsub.py` — topic resolution, bytes, attributes, and acknowledgement.
- `tests/integration/factories.py` — unique User/Workspace test-scope creation.
- `tests/integration/events/test_service.py` — atomic and idempotent ingestion.
- `tests/integration/events/test_outbox.py` — relay claims, leases, success, failure, and stale claims.
- `tests/integration/events/test_processor.py` — handler dispatch, idempotency, failure, claims, and leases.
- `tests/unit/test_worker.py` — composition and single-message dispatch.

### Modified files

- `pyproject.toml` and `uv.lock` — add the official Google Pub/Sub client.
- `src/eva_ai/config.py` — typed project/topic, batch-size, and lease settings.
- `migrations/env.py` — import ORM models before assigning Alembic metadata.
- `tests/integration/conftest.py` — provide a reusable async Database fixture.
- `tests/integration/test_migrations.py` — assert all Milestone 1 tables and constraints exist.
- `README.md` and `.env.example` — document the backbone, local configuration, and intentional GCP deferral.

---

### Task 1: Canonical Event Types, Safe Errors, and Configuration

**Files:**
- Create: `src/eva_ai/events/__init__.py`
- Create: `src/eva_ai/events/types.py`
- Create: `src/eva_ai/events/errors.py`
- Modify: `src/eva_ai/config.py`
- Test: `tests/unit/events/test_types.py`
- Test: `tests/unit/events/test_errors.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: Pydantic `BaseModel`, `JsonValue`, and the existing `Settings` model.
- Produces: `NewEvent`, `EventAvailableMessage`, `OutboundMessage`, `PrincipalType`, `ProcessingStage`, `OutboxState`, `StoredError`, `BackboneError`, `ScopeMismatchError`, `UnknownEventError`, `StaleClaimError`, `sanitize_error(error: BaseException) -> StoredError`, and the new Settings fields.

- [ ] **Step 1: Write failing canonical-type and error-safety tests**

```python
# tests/unit/events/test_types.py
from datetime import UTC, datetime
from uuid import UUID, uuid7

import pytest
from pydantic import ValidationError

from eva_ai.events.types import EventAvailableMessage, NewEvent, PrincipalType


def valid_event() -> dict[str, object]:
    return {
        "user_id": uuid7(),
        "workspace_id": uuid7(),
        "source": "gmail",
        "event_type": "email.received",
        "idempotency_key": "gmail:message-123",
        "occurred_at": datetime(2026, 8, 30, 8, 0, tzinfo=UTC),
        "principal_type": PrincipalType.USER,
        "payload": {"message_id": "message-123"},
    }


def test_new_event_is_immutable_and_generates_uuid7() -> None:
    event = NewEvent.model_validate(valid_event())
    assert event.id.version == 7
    with pytest.raises(ValidationError):
        event.source = "calendar"  # type: ignore[misc]


@pytest.mark.parametrize("field", ["source", "event_type", "idempotency_key"])
def test_new_event_rejects_blank_identifiers(field: str) -> None:
    values = valid_event()
    values[field] = "   "
    with pytest.raises(ValidationError):
        NewEvent.model_validate(values)


def test_new_event_rejects_naive_datetimes_and_non_positive_versions() -> None:
    values = valid_event() | {"occurred_at": datetime(2026, 8, 30), "schema_version": 0}
    with pytest.raises(ValidationError):
        NewEvent.model_validate(values)


def test_event_available_message_round_trips_as_json() -> None:
    message = EventAvailableMessage(
        outbox_message_id=uuid7(),
        event_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        event_type="email.received",
        schema_version=1,
    )
    decoded = EventAvailableMessage.model_validate_json(message.model_dump_json())
    assert decoded == message
    assert isinstance(decoded.event_id, UUID)
```

```python
# tests/unit/events/test_errors.py
from eva_ai.events.errors import sanitize_error


def test_sanitize_error_never_persists_exception_text() -> None:
    stored = sanitize_error(
        RuntimeError("postgresql://eva:secret@db/eva?token=top-secret")
    )
    assert stored.error_type == "RuntimeError"
    assert stored.summary == "operation failed"
    assert "secret" not in stored.model_dump_json()
```

```python
# append to tests/unit/test_config.py
def test_event_backbone_settings_have_safe_local_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.pubsub_project_id is None
    assert settings.pubsub_topic_id == "eva-events"
    assert settings.outbox_batch_limit == 100
    assert settings.outbox_lease_seconds == 60
    assert settings.processing_lease_seconds == 300


def test_event_backbone_settings_reject_non_positive_limits() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, outbox_batch_limit=0)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/unit/events/test_types.py tests/unit/events/test_errors.py tests/unit/test_config.py -v`

Expected: collection fails because `eva_ai.events` does not exist and Settings lacks the new fields.

- [ ] **Step 3: Implement the validated types and secret-safe errors**

```python
# src/eva_ai/events/types.py
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid7

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class PrincipalType(StrEnum):
    USER = "USER"
    SYSTEM = "SYSTEM"
    EXTERNAL = "EXTERNAL"


class ProcessingStage(StrEnum):
    RECEIVED = "RECEIVED"
    NORMALIZED = "NORMALIZED"
    ENRICHED = "ENRICHED"
    CLASSIFIED = "CLASSIFIED"
    CORRELATED = "CORRELATED"
    HANDLED = "HANDLED"


class OutboxState(StrEnum):
    PENDING = "PENDING"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"


class NewEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid7)
    user_id: UUID
    workspace_id: UUID
    source: str
    event_type: str
    external_id: str | None = None
    idempotency_key: str
    occurred_at: datetime
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    principal_type: PrincipalType
    principal_id: UUID | None = None
    actor: dict[str, JsonValue] | None = None
    subject: dict[str, JsonValue] | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    correlation_keys: list[str] = Field(default_factory=list)
    schema_version: int = Field(default=1, gt=0)

    @field_validator("source", "event_type", "idempotency_key")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def require_aware_timestamps(self) -> Self:
        for value in (self.occurred_at, self.received_at):
            if value.utcoffset() is None:
                raise ValueError("timestamps must include a timezone")
        return self


class EventAvailableMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    outbox_message_id: UUID
    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    event_type: str
    schema_version: int = Field(gt=0)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    outbox_message_id: UUID
    destination: str
    envelope: EventAvailableMessage
```

```python
# src/eva_ai/events/errors.py
from pydantic import BaseModel, ConfigDict


class BackboneError(RuntimeError):
    pass


class UnknownEventError(BackboneError):
    pass


class ScopeMismatchError(BackboneError):
    pass


class StaleClaimError(BackboneError):
    pass


class StoredError(BaseModel):
    model_config = ConfigDict(frozen=True)
    error_type: str
    summary: str


def sanitize_error(error: BaseException) -> StoredError:
    # Exception messages can contain DSNs or provider payloads, so persistence keeps no text.
    return StoredError(error_type=type(error).__name__, summary="operation failed")
```

Add the following annotated fields to `Settings` and import `PositiveInt`:

```python
    pubsub_project_id: str | None = None
    pubsub_topic_id: str = "eva-events"
    outbox_batch_limit: PositiveInt = 100
    outbox_lease_seconds: PositiveInt = 60
    processing_lease_seconds: PositiveInt = 300
```

Export the public types and errors explicitly from `src/eva_ai/events/__init__.py` through `__all__`.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `uv run pytest tests/unit/events/test_types.py tests/unit/events/test_errors.py tests/unit/test_config.py -v`

Expected: all focused tests pass.

- [ ] **Step 5: Run static checks and commit**

Run: `uv run ruff format src/eva_ai/events src/eva_ai/config.py tests/unit/events tests/unit/test_config.py && uv run ruff check src/eva_ai/events src/eva_ai/config.py tests/unit/events tests/unit/test_config.py && uv run mypy src/eva_ai/events src/eva_ai/config.py tests/unit/events tests/unit/test_config.py`

```bash
git add src/eva_ai/events src/eva_ai/config.py tests/unit/events tests/unit/test_config.py
git commit -m "feat: define canonical event contracts"
```

---

### Task 2: PostgreSQL Event-Backbone Schema

**Files:**
- Create: `src/eva_ai/db/models/__init__.py`
- Create: `src/eva_ai/db/models/common.py`
- Create: `src/eva_ai/db/models/identity.py`
- Create: `src/eva_ai/db/models/events.py`
- Create: `migrations/versions/20260830_0002_event_backbone.py`
- Modify: `migrations/env.py`
- Modify: `tests/integration/conftest.py`
- Modify: `tests/integration/test_migrations.py`
- Create: `tests/integration/factories.py`
- Test: `tests/integration/events/test_schema.py`

**Interfaces:**
- Consumes: `Base`, the enums from `eva_ai.events.types`, and the existing `Database` session factory.
- Produces: ORM `User`, `Workspace`, `Event`, `EventProcessing`, `OutboxMessage`, `TimestampMixin`, `Scope`, `create_scope(database: Database) -> Scope`, and Alembic revision `20260830_0002` with `down_revision = "20260830_0001"`.

- [ ] **Step 1: Write failing migration and scope-constraint tests**

```python
# add to tests/integration/test_migrations.py
@pytest.mark.integration
async def test_event_backbone_tables_exist(database: Database) -> None:
    async with database.engine.connect() as connection:
        tables = await connection.run_sync(lambda sync_connection: inspect(sync_connection).get_table_names())
    assert {
        "users",
        "workspaces",
        "events",
        "event_processing",
        "outbox_messages",
    } <= set(tables)
```

```python
# tests/integration/events/test_schema.py
from datetime import UTC, datetime
from uuid import uuid7

import pytest
from sqlalchemy.exc import IntegrityError

from eva_ai.db.models import Event, User, Workspace
from eva_ai.events.types import PrincipalType


@pytest.mark.integration
async def test_event_cannot_claim_another_users_workspace(database) -> None:
    first_user_id, second_user_id, workspace_id = uuid7(), uuid7(), uuid7()
    async with database.session() as session:
        async with session.begin():
            session.add_all(
                [
                    User(id=first_user_id, display_name="First"),
                    User(id=second_user_id, display_name="Second"),
                    Workspace(id=workspace_id, user_id=first_user_id, name="Personal"),
                ]
            )
    with pytest.raises(IntegrityError):
        async with database.session() as session:
            async with session.begin():
                session.add(
                    Event(
                        id=uuid7(),
                        user_id=second_user_id,
                        workspace_id=workspace_id,
                        source="test",
                        event_type="test.created",
                        idempotency_key=f"test:{uuid7()}",
                        occurred_at=datetime.now(UTC),
                        received_at=datetime.now(UTC),
                        principal_type=PrincipalType.USER,
                        payload={},
                        event_metadata={},
                        correlation_keys=[],
                        schema_version=1,
                    )
                )
```

- [ ] **Step 2: Run migration tests and verify RED**

Run: `docker compose up -d --wait postgres && uv run pytest tests/integration/test_migrations.py tests/integration/events/test_schema.py -v`

Expected: tests fail because the Milestone 1 tables and model package do not exist.

- [ ] **Step 3: Implement focused ORM models**

Use concrete `Mapped[T]` annotations with `mapped_column` throughout. The model definitions must encode this exact schema:

```python
# src/eva_ai/db/models/common.py
from datetime import UTC, datetime
from uuid import UUID, uuid7

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
```

```python
# src/eva_ai/db/models/identity.py
class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    display_name: Mapped[str] = mapped_column(String(200))


class Workspace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_workspaces_id_user_id"),
        UniqueConstraint("user_id", "name", name="uq_workspaces_user_id_name"),
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
```

```python
# src/eva_ai/db/models/events.py — table-level invariants
EVENT_SCOPE_FK = ForeignKeyConstraint(
    ["workspace_id", "user_id"],
    ["workspaces.id", "workspaces.user_id"],
    name="fk_events_workspace_user",
    ondelete="CASCADE",
)


class Event(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        EVENT_SCOPE_FK,
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_events_workspace_idempotency"
        ),
        CheckConstraint("schema_version > 0", name="ck_events_schema_version_positive"),
        CheckConstraint(
            "principal_type IN ('USER', 'SYSTEM', 'EXTERNAL')",
            name="ck_events_principal_type",
        ),
    )
    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    source: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(200))
    external_id: Mapped[str | None] = mapped_column(String(500))
    idempotency_key: Mapped[str] = mapped_column(String(500))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    principal_type: Mapped[PrincipalType] = mapped_column(String(32))
    principal_id: Mapped[UUID | None]
    actor: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    subject: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    event_metadata: Mapped[dict[str, JsonValue]] = mapped_column("metadata", JSONB)
    correlation_keys: Mapped[list[str]] = mapped_column(ARRAY(Text))
    schema_version: Mapped[int]


class EventProcessing(TimestampMixin, Base):
    __tablename__ = "event_processing"
    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="ck_event_processing_attempts"),
        CheckConstraint(
            "stage IN ('RECEIVED', 'NORMALIZED', 'ENRICHED', 'CLASSIFIED', "
            "'CORRELATED', 'HANDLED')",
            name="ck_event_processing_stage",
        ),
    )
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    stage: Mapped[ProcessingStage] = mapped_column(String(32))
    attempt_count: Mapped[int] = mapped_column(default=0)
    last_error_type: Mapped[str | None] = mapped_column(String(200))
    last_error_summary: Mapped[str | None] = mapped_column(String(500))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_id: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="ck_outbox_attempts"),
        CheckConstraint("schema_version > 0", name="ck_outbox_schema_version_positive"),
        CheckConstraint(
            "state IN ('PENDING', 'PUBLISHING', 'PUBLISHED')", name="ck_outbox_state"
        ),
    )
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    destination: Mapped[str] = mapped_column(String(200))
    message_type: Mapped[str] = mapped_column(String(200))
    schema_version: Mapped[int]
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    state: Mapped[OutboxState] = mapped_column(String(32))
    attempt_count: Mapped[int] = mapped_column(default=0)
    last_error_type: Mapped[str | None] = mapped_column(String(200))
    last_error_summary: Mapped[str | None] = mapped_column(String(500))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claim_id: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

Add indexes on `events(workspace_id, occurred_at)`, `outbox_messages(state, available_at)`, and `event_processing(stage, next_retry_at)`. `db/models/__init__.py` must import and export all five models. Add `from eva_ai.db import models as models` to `migrations/env.py` before `target_metadata = Base.metadata` so Alembic loads every table.

- [ ] **Step 4: Write the explicit Alembic revision**

Create all five tables in dependency order with the columns, named constraints, foreign-key actions, and indexes above. Use PostgreSQL-native `UUID`, `JSONB`, and `ARRAY(sa.Text())`. The downgrade must drop indexes and tables in this exact reverse order:

```python
def downgrade() -> None:
    op.drop_index("ix_event_processing_stage_retry", table_name="event_processing")
    op.drop_index("ix_outbox_state_available", table_name="outbox_messages")
    op.drop_index("ix_events_workspace_occurred", table_name="events")
    op.drop_table("outbox_messages")
    op.drop_table("event_processing")
    op.drop_table("events")
    op.drop_table("workspaces")
    op.drop_table("users")
```

The migration's `upgrade()` must use `server_default=sa.text("now()")` for timestamps, `server_default="0"` for attempt counts, `server_default="RECEIVED"` and `server_default="PENDING"` for states, and remove no defaults afterward because they are valid database invariants.

- [ ] **Step 5: Add integration helpers and run GREEN**

```python
# tests/integration/conftest.py additions
@pytest.fixture
async def database() -> AsyncIterator[Database]:
    database = Database(get_settings().database_url.get_secret_value())
    try:
        yield database
    finally:
        await database.close()
```

```python
# tests/integration/factories.py
from dataclasses import dataclass
from uuid import UUID, uuid7

from eva_ai.db.models import User, Workspace
from eva_ai.db.session import Database


@dataclass(frozen=True, slots=True)
class Scope:
    user_id: UUID
    workspace_id: UUID


async def create_scope(database: Database) -> Scope:
    scope = Scope(user_id=uuid7(), workspace_id=uuid7())
    async with database.session() as session:
        async with session.begin():
            session.add(User(id=scope.user_id, display_name=f"User {scope.user_id}"))
            session.add(
                Workspace(
                    id=scope.workspace_id,
                    user_id=scope.user_id,
                    name=f"Workspace {scope.workspace_id}",
                )
            )
    return scope
```

Run: `uv run alembic upgrade head && uv run pytest tests/integration/test_migrations.py tests/integration/events/test_schema.py -v`

Expected: both files pass and PostgreSQL rejects the cross-user Event.

- [ ] **Step 6: Verify downgrade/upgrade and commit**

Run: `uv run alembic downgrade 20260830_0001 && uv run alembic upgrade head && uv run ruff format src/eva_ai/db migrations tests/integration && uv run ruff check src/eva_ai/db migrations tests/integration && uv run mypy src/eva_ai/db migrations tests/integration`

```bash
git add src/eva_ai/db/models migrations tests/integration
git commit -m "feat: add event backbone schema"
```

---

### Task 3: Atomic and Idempotent Event Ingestion

**Files:**
- Create: `src/eva_ai/events/service.py`
- Modify: `src/eva_ai/events/__init__.py`
- Test: `tests/integration/events/test_service.py`

**Interfaces:**
- Consumes: `Database`, `NewEvent`, `EventAvailableMessage`, and ORM Event/Processing/Outbox models.
- Produces: `IngestResult(event_id: UUID, created: bool)` and `EventService(database: Database, destination: str)` with `async ingest(command: NewEvent) -> IngestResult`.

- [ ] **Step 1: Write failing happy-path and sequential-idempotency tests**

```python
# tests/integration/events/test_service.py
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from eva_ai.db.models import Event, EventProcessing, OutboxMessage
from eva_ai.events.service import EventService
from eva_ai.events.types import NewEvent, OutboxState, PrincipalType, ProcessingStage
from tests.integration.factories import create_scope


async def backbone_counts(database, workspace_id: UUID) -> tuple[int, int, int]:
    async with database.session() as session:
        event_count = await session.scalar(
            select(func.count()).select_from(Event).where(Event.workspace_id == workspace_id)
        )
        processing_count = await session.scalar(
            select(func.count())
            .select_from(EventProcessing)
            .join(Event, Event.id == EventProcessing.event_id)
            .where(Event.workspace_id == workspace_id)
        )
        outbox_count = await session.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .join(Event, Event.id == OutboxMessage.event_id)
            .where(Event.workspace_id == workspace_id)
        )
    return int(event_count or 0), int(processing_count or 0), int(outbox_count or 0)


@pytest.mark.integration
async def test_ingest_creates_event_processing_and_outbox_atomically(database) -> None:
    scope = await create_scope(database)
    command = NewEvent(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        source="gmail",
        event_type="email.received",
        idempotency_key="gmail:atomic-1",
        occurred_at=datetime.now(UTC),
        principal_type=PrincipalType.USER,
        payload={"message_id": "atomic-1"},
    )
    result = await EventService(database, "eva-events").ingest(command)
    async with database.session() as session:
        event = await session.get(Event, result.event_id)
        processing = await session.get(EventProcessing, result.event_id)
        outbox = await session.scalar(
            select(OutboxMessage).where(OutboxMessage.event_id == result.event_id)
        )
    assert result.created is True
    assert event is not None and event.payload == {"message_id": "atomic-1"}
    assert processing is not None and processing.stage == ProcessingStage.RECEIVED
    assert outbox is not None and outbox.state == OutboxState.PENDING
    assert outbox.payload["event_id"] == str(result.event_id)


@pytest.mark.integration
async def test_duplicate_ingest_returns_existing_event_without_more_children(database) -> None:
    scope = await create_scope(database)
    first = NewEvent(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        source="test",
        event_type="test.created",
        idempotency_key="same-key",
        occurred_at=datetime.now(UTC),
        principal_type=PrincipalType.USER,
    )
    service = EventService(database, "eva-events")
    first_result = await service.ingest(first)
    second_result = await service.ingest(first.model_copy(update={"id": uuid7()}))
    counts = await backbone_counts(database, scope.workspace_id)
    assert second_result == IngestResult(event_id=first_result.event_id, created=False)
    assert counts == (1, 1, 1)
```

Use a unique workspace in each test, and scope count queries by Event workspace or Event ID so accumulated integration fixtures do not affect assertions.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/integration/events/test_service.py -v`

Expected: collection fails because `EventService` and `IngestResult` do not exist.

- [ ] **Step 3: Implement the one-transaction ingestion service**

```python
# src/eva_ai/events/service.py
from dataclasses import dataclass
from uuid import UUID, uuid7

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from eva_ai.db.models import Event, EventProcessing, OutboxMessage
from eva_ai.db.session import Database
from eva_ai.events.types import (
    EventAvailableMessage,
    NewEvent,
    OutboxState,
    ProcessingStage,
)


@dataclass(frozen=True, slots=True)
class IngestResult:
    event_id: UUID
    created: bool


class EventService:
    def __init__(self, database: Database, destination: str) -> None:
        self._database = database
        self._destination = destination

    async def ingest(self, command: NewEvent) -> IngestResult:
        async with self._database.session() as session:
            async with session.begin():
                statement = (
                    insert(Event)
                    .values(
                        id=command.id,
                        user_id=command.user_id,
                        workspace_id=command.workspace_id,
                        source=command.source,
                        event_type=command.event_type,
                        external_id=command.external_id,
                        idempotency_key=command.idempotency_key,
                        occurred_at=command.occurred_at,
                        received_at=command.received_at,
                        principal_type=command.principal_type,
                        principal_id=command.principal_id,
                        actor=command.actor,
                        subject=command.subject,
                        payload=command.payload,
                        metadata=command.metadata,
                        correlation_keys=command.correlation_keys,
                        schema_version=command.schema_version,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[Event.workspace_id, Event.idempotency_key]
                    )
                    .returning(Event.id)
                )
                event_id = (await session.execute(statement)).scalar_one_or_none()
                if event_id is None:
                    existing_id = await session.scalar(
                        select(Event.id).where(
                            Event.workspace_id == command.workspace_id,
                            Event.idempotency_key == command.idempotency_key,
                        )
                    )
                    if existing_id is None:
                        raise RuntimeError("conflicting event was not visible")
                    return IngestResult(event_id=existing_id, created=False)

                outbox_id = uuid7()
                envelope = EventAvailableMessage(
                    outbox_message_id=outbox_id,
                    event_id=event_id,
                    user_id=command.user_id,
                    workspace_id=command.workspace_id,
                    event_type=command.event_type,
                    schema_version=1,
                )
                # These child rows share the Event transaction; none can survive a rollback alone.
                session.add(
                    EventProcessing(event_id=event_id, stage=ProcessingStage.RECEIVED)
                )
                session.add(
                    OutboxMessage(
                        id=outbox_id,
                        event_id=event_id,
                        destination=self._destination,
                        message_type="event.available",
                        schema_version=1,
                        payload=envelope.model_dump(mode="json"),
                        state=OutboxState.PENDING,
                        available_at=command.received_at,
                    )
                )
                return IngestResult(event_id=event_id, created=True)
```

- [ ] **Step 4: Add concurrent duplicate and rollback tests**

```python
@pytest.mark.integration
async def test_concurrent_duplicate_ingest_creates_one_backbone(database) -> None:
    scope = await create_scope(database)
    command = make_event(scope, idempotency_key="concurrent-key")
    first, second = await asyncio.gather(
        EventService(database, "eva-events").ingest(command),
        EventService(database, "eva-events").ingest(
            command.model_copy(update={"id": uuid7()})
        ),
    )
    assert first.event_id == second.event_id
    assert sorted([first.created, second.created]) == [False, True]
    assert await backbone_counts(database, scope.workspace_id) == (1, 1, 1)


@pytest.mark.integration
async def test_invalid_scope_rolls_back_the_whole_backbone(database) -> None:
    scope = await create_scope(database)
    command = make_event(scope, user_id=uuid7(), idempotency_key="invalid-scope")
    with pytest.raises(IntegrityError):
        await EventService(database, "eva-events").ingest(command)
    assert await backbone_counts(database, scope.workspace_id) == (0, 0, 0)
```

Implement `make_event` and `backbone_counts` in the test file with explicit workspace-scoped joins for all three counts.

- [ ] **Step 5: Run all service tests and commit**

Run: `uv run pytest tests/integration/events/test_service.py -v && uv run ruff check src/eva_ai/events tests/integration/events/test_service.py && uv run mypy src/eva_ai/events tests/integration/events/test_service.py`

Expected: happy path, sequential duplicate, concurrent duplicate, and rollback tests pass.

```bash
git add src/eva_ai/events tests/integration/events/test_service.py
git commit -m "feat: persist events with transactional outbox"
```

---

### Task 4: Publisher Boundary and Google Pub/Sub Adapter

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/eva_ai/events/publisher.py`
- Create: `src/eva_ai/integrations/__init__.py`
- Create: `src/eva_ai/integrations/gcp/__init__.py`
- Create: `src/eva_ai/integrations/gcp/pubsub.py`
- Test: `tests/unit/events/test_publisher.py`
- Test: `tests/unit/integrations/gcp/test_pubsub.py`

**Interfaces:**
- Consumes: `OutboundMessage` from Task 1 and `google.cloud.pubsub_v1.PublisherClient`.
- Produces: `Publisher.publish(message: OutboundMessage) -> str`, `InMemoryPublisher.messages`, `GooglePubSubPublisher(project_id: str, client: PubSubClient | None = None)`, `PubSubClient`, and `PublishFuture` protocols.

- [ ] **Step 1: Add the locked dependency**

Run: `uv add 'google-cloud-pubsub>=2.39,<3'`

Expected: `pyproject.toml` and `uv.lock` include a Pub/Sub 2.x client compatible with Python 3.14.

- [ ] **Step 2: Write failing publisher contract tests**

```python
# tests/unit/events/test_publisher.py
@pytest.mark.asyncio
async def test_in_memory_publisher_records_messages_and_returns_stable_id() -> None:
    message = outbound_message()
    publisher = InMemoryPublisher()
    assert await publisher.publish(message) == f"in-memory:{message.outbox_message_id}"
    assert publisher.messages == [message]
```

```python
# tests/unit/integrations/gcp/test_pubsub.py
class FakeFuture:
    def result(self, timeout: float | None = None) -> str:
        assert timeout is None
        return "provider-message-42"


class FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, dict[str, str]]] = []

    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic: str, data: bytes, **attrs: str) -> FakeFuture:
        self.published.append((topic, data, attrs))
        return FakeFuture()


@pytest.mark.asyncio
async def test_google_publisher_serializes_envelope_and_awaits_ack() -> None:
    client = FakeClient()
    message = outbound_message(destination="eva-events")
    provider_id = await GooglePubSubPublisher("eva-project", client).publish(message)
    topic, data, attrs = client.published[0]
    assert provider_id == "provider-message-42"
    assert topic == "projects/eva-project/topics/eva-events"
    assert json.loads(data) == message.envelope.model_dump(mode="json")
    assert attrs == {
        "message_type": "event.available",
        "event_id": str(message.envelope.event_id),
        "workspace_id": str(message.envelope.workspace_id),
    }
```

- [ ] **Step 3: Run focused tests and verify RED**

Run: `uv run pytest tests/unit/events/test_publisher.py tests/unit/integrations/gcp/test_pubsub.py -v`

Expected: imports fail because publisher implementations do not exist.

- [ ] **Step 4: Implement the local and Google publishers**

```python
# src/eva_ai/events/publisher.py
from typing import Protocol

from eva_ai.events.types import OutboundMessage


class Publisher(Protocol):
    async def publish(self, message: OutboundMessage) -> str:
        raise NotImplementedError


class InMemoryPublisher:
    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    async def publish(self, message: OutboundMessage) -> str:
        self.messages.append(message)
        return f"in-memory:{message.outbox_message_id}"
```

```python
# src/eva_ai/integrations/gcp/pubsub.py
import asyncio
from typing import Protocol

from google.cloud import pubsub_v1

from eva_ai.events.types import OutboundMessage


class PublishFuture(Protocol):
    def result(self, timeout: float | None = None) -> str:
        raise NotImplementedError


class PubSubClient(Protocol):
    def topic_path(self, project: str, topic: str) -> str:
        raise NotImplementedError

    def publish(self, topic: str, data: bytes, **attrs: str) -> PublishFuture:
        raise NotImplementedError


class GooglePubSubPublisher:
    def __init__(self, project_id: str, client: PubSubClient | None = None) -> None:
        self._project_id = project_id
        self._client = client or pubsub_v1.PublisherClient()

    async def publish(self, message: OutboundMessage) -> str:
        topic = self._client.topic_path(self._project_id, message.destination)
        data = message.envelope.model_dump_json().encode("utf-8")
        future = self._client.publish(
            topic,
            data,
            message_type="event.available",
            event_id=str(message.envelope.event_id),
            workspace_id=str(message.envelope.workspace_id),
        )
        # Pub/Sub returns its own Future, so wait in a thread instead of blocking asyncio.
        return await asyncio.to_thread(future.result)
```

- [ ] **Step 5: Run tests, static checks, and commit**

Run: `uv run pytest tests/unit/events/test_publisher.py tests/unit/integrations/gcp/test_pubsub.py -v && uv run ruff format src tests/unit && uv run ruff check src tests/unit && uv run mypy src tests/unit`

```bash
git add pyproject.toml uv.lock src/eva_ai/events/publisher.py src/eva_ai/integrations tests/unit/events/test_publisher.py tests/unit/integrations
git commit -m "feat: add Pub/Sub publisher boundary"
```

---

### Task 5: Lease-Based Outbox Relay

**Files:**
- Create: `src/eva_ai/events/outbox.py`
- Modify: `src/eva_ai/events/__init__.py`
- Test: `tests/integration/events/test_outbox.py`

**Interfaces:**
- Consumes: `Database`, `Publisher`, `OutboundMessage`, `EventAvailableMessage`, `OutboxMessage`, `OutboxState`, `sanitize_error`, and `StaleClaimError`.
- Produces: `ClaimedOutboxMessage`, `PublishBatchResult`, and `OutboxRelay(database: Database, publisher: Publisher, lease_seconds: int)` with `claim_batch(limit: int, now: datetime | None = None) -> list[ClaimedOutboxMessage]`, `complete_claim(message_id: UUID, claim_id: UUID, provider_message_id: str, published_at: datetime | None = None) -> None`, `release_claim(message_id: UUID, claim_id: UUID, error: BaseException) -> None`, and `publish_batch(limit: int) -> PublishBatchResult`.

- [ ] **Step 1: Write failing claim exclusivity and lease-reclamation tests**

```python
@pytest.mark.integration
async def test_two_relays_claim_disjoint_rows(database) -> None:
    await create_pending_messages(database, count=4)
    relay_one = OutboxRelay(database, InMemoryPublisher(), lease_seconds=60)
    relay_two = OutboxRelay(database, InMemoryPublisher(), lease_seconds=60)
    first, second = await asyncio.gather(relay_one.claim_batch(2), relay_two.claim_batch(2))
    assert len(first) == len(second) == 2
    assert {item.id for item in first}.isdisjoint(item.id for item in second)
    assert all(item.claim_id is not None for item in first + second)


@pytest.mark.integration
async def test_expired_outbox_lease_is_reclaimable(database) -> None:
    message_id = await create_pending_message(database)
    relay = OutboxRelay(database, InMemoryPublisher(), lease_seconds=60)
    first = (await relay.claim_batch(1, now=FIXED_NOW))[0]
    second = (await relay.claim_batch(1, now=FIXED_NOW + timedelta(seconds=61)))[0]
    assert first.id == second.id == message_id
    assert first.claim_id != second.claim_id
    assert second.attempt_count == 2
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/integration/events/test_outbox.py -v`

Expected: collection fails because `OutboxRelay` does not exist.

- [ ] **Step 3: Implement claim DTOs and the short claim transaction**

```python
@dataclass(frozen=True, slots=True)
class ClaimedOutboxMessage:
    id: UUID
    claim_id: UUID
    event_id: UUID
    destination: str
    envelope: EventAvailableMessage
    attempt_count: int

    def outbound(self) -> OutboundMessage:
        return OutboundMessage(self.id, self.destination, self.envelope)


@dataclass(frozen=True, slots=True)
class PublishBatchResult:
    claimed: int
    published: int
    failed: int
```

Implement `claim_batch` with this exact eligibility and lock shape:

```python
eligible = or_(
    and_(
        OutboxMessage.state == OutboxState.PENDING,
        OutboxMessage.available_at <= effective_now,
    ),
    and_(
        OutboxMessage.state == OutboxState.PUBLISHING,
        OutboxMessage.lease_expires_at <= effective_now,
    ),
)
statement = (
    select(OutboxMessage)
    .where(eligible)
    .order_by(OutboxMessage.available_at, OutboxMessage.created_at)
    .limit(limit)
    .with_for_update(skip_locked=True)
)
```

Inside `async with session.begin()`, assign a fresh `uuid7()` claim to each selected row, set `PUBLISHING`, increment `attempt_count`, set `lease_expires_at = effective_now + timedelta(seconds=self._lease_seconds)`, clear the stored error, validate `EventAvailableMessage.model_validate(row.payload)`, and return immutable snapshots only after the transaction exits. Add a comment that the network publish deliberately occurs later.

- [ ] **Step 4: Write failing success, failure, and stale-claim tests**

```python
@pytest.mark.integration
async def test_publish_batch_marks_acknowledged_message_published(database) -> None:
    message_id = await create_pending_message(database)
    result = await OutboxRelay(database, InMemoryPublisher(), 60).publish_batch(10)
    row = await load_outbox(database, message_id)
    assert result == PublishBatchResult(claimed=1, published=1, failed=0)
    assert row.state == OutboxState.PUBLISHED
    assert row.provider_message_id == f"in-memory:{message_id}"
    assert row.claim_id is None and row.lease_expires_at is None
    assert row.published_at is not None


@pytest.mark.integration
async def test_publish_failure_releases_message_without_storing_secret(database) -> None:
    message_id = await create_pending_message(database)
    result = await OutboxRelay(database, FailingPublisher(), 60).publish_batch(10)
    row = await load_outbox(database, message_id)
    assert result == PublishBatchResult(claimed=1, published=0, failed=1)
    assert row.state == OutboxState.PENDING
    assert row.last_error_type == "RuntimeError"
    assert row.last_error_summary == "operation failed"
    assert "provider-token" not in f"{row.last_error_type}:{row.last_error_summary}"


@pytest.mark.integration
async def test_stale_claim_cannot_complete_reclaimed_message(database) -> None:
    relay = OutboxRelay(database, InMemoryPublisher(), 60)
    old = (await relay.claim_batch(1, now=FIXED_NOW))[0]
    await relay.claim_batch(1, now=FIXED_NOW + timedelta(seconds=61))
    with pytest.raises(StaleClaimError):
        await relay.complete_claim(old.id, old.claim_id, "late-provider-id", FIXED_NOW)
```

- [ ] **Step 5: Implement claim-protected completion, release, and publication**

Both mutation methods must issue an `UPDATE` constrained by all of:

```python
where(
    OutboxMessage.id == message_id,
    OutboxMessage.state == OutboxState.PUBLISHING,
    OutboxMessage.claim_id == claim_id,
)
```

`complete_claim` sets `PUBLISHED`, provider ID, `published_at`, clears claim/lease/error, and raises `StaleClaimError` when `rowcount != 1`. `release_claim` sets `PENDING`, clears claim/lease, stores `sanitize_error(error)`, and raises `StaleClaimError` when `rowcount != 1`.

```python
async def publish_batch(self, limit: int) -> PublishBatchResult:
    claimed = await self.claim_batch(limit)
    published = 0
    failed = 0
    for item in claimed:
        try:
            provider_id = await self._publisher.publish(item.outbound())
            await self.complete_claim(item.id, item.claim_id, provider_id)
            published += 1
        except Exception as error:
            await self.release_claim(item.id, item.claim_id, error)
            failed += 1
    return PublishBatchResult(len(claimed), published, failed)
```

Use structured logger extras containing `event_id`, `outbox_message_id`, `claim_id`, and `outcome`; never log `payload` or the exception message.

- [ ] **Step 6: Run relay tests and commit**

Run: `uv run pytest tests/integration/events/test_outbox.py -v && uv run ruff check src/eva_ai/events/outbox.py tests/integration/events/test_outbox.py && uv run mypy src/eva_ai/events/outbox.py tests/integration/events/test_outbox.py`

Expected: exclusivity, lease expiry, acknowledgement, safe failure release, and stale-claim protection pass.

```bash
git add src/eva_ai/events tests/integration/events/test_outbox.py
git commit -m "feat: publish outbox messages with leases"
```

---

### Task 6: Idempotent Event Processor and Handler Contract

**Files:**
- Create: `src/eva_ai/events/processor.py`
- Modify: `src/eva_ai/events/__init__.py`
- Test: `tests/integration/events/test_processor.py`

**Interfaces:**
- Consumes: `Database`, `EventAvailableMessage`, `Event`, `EventProcessing`, `ProcessingStage`, `sanitize_error`, `UnknownEventError`, `ScopeMismatchError`, and `StaleClaimError`.
- Produces: `StoredEvent`, `EventHandler.handle(event: StoredEvent) -> None`, `ProcessOutcome`, `ProcessResult`, and `EventProcessor(database, lease_seconds).process(message, handler, now=None) -> ProcessResult`.

- [ ] **Step 1: Write failing success, redelivery, and scope-safety tests**

```python
class RecordingHandler:
    def __init__(self) -> None:
        self.event_ids: list[UUID] = []

    async def handle(self, event: StoredEvent) -> None:
        self.event_ids.append(event.id)


@pytest.mark.integration
async def test_processor_handles_event_and_redelivery_is_idempotent(database) -> None:
    message = await ingest_message(database)
    handler = RecordingHandler()
    processor = EventProcessor(database, lease_seconds=300)
    first = await processor.process(message, handler)
    second = await processor.process(message, handler)
    row = await load_processing(database, message.event_id)
    assert first.outcome == ProcessOutcome.HANDLED
    assert second.outcome == ProcessOutcome.ALREADY_HANDLED
    assert handler.event_ids == [message.event_id]
    assert row.stage == ProcessingStage.HANDLED
    assert row.attempt_count == 1


@pytest.mark.integration
async def test_processor_rejects_scope_mismatch_without_handler(database) -> None:
    message = await ingest_message(database)
    handler = RecordingHandler()
    with pytest.raises(ScopeMismatchError):
        await EventProcessor(database, 300).process(
            message.model_copy(update={"workspace_id": uuid7()}), handler
        )
    assert handler.event_ids == []
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/integration/events/test_processor.py -v`

Expected: collection fails because the processor and handler types do not exist.

- [ ] **Step 3: Implement processor types and claim transaction**

```python
class ProcessOutcome(StrEnum):
    HANDLED = "HANDLED"
    ALREADY_HANDLED = "ALREADY_HANDLED"
    BUSY = "BUSY"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    event_id: UUID
    outcome: ProcessOutcome


@dataclass(frozen=True, slots=True)
class StoredEvent:
    id: UUID
    user_id: UUID
    workspace_id: UUID
    source: str
    event_type: str
    payload: dict[str, JsonValue]
    schema_version: int


class EventHandler(Protocol):
    async def handle(self, event: StoredEvent) -> None:
        raise NotImplementedError
```

Implement `_claim(message, now)` in a short transaction:

1. Select Event joined to EventProcessing by `message.event_id` with `EventProcessing` locked `FOR UPDATE`.
2. Raise `UnknownEventError` when no row exists.
3. Compare both persisted IDs with `message.user_id` and `message.workspace_id`; raise `ScopeMismatchError` before any handler call.
4. Return `ALREADY_HANDLED` without incrementing attempts when stage is `HANDLED`.
5. Return `BUSY` when `claim_id` is set and `lease_expires_at > now`.
6. Otherwise assign a new UUIDv7 claim, increment attempts, set the lease, clear stored errors, and return an immutable StoredEvent plus claim ID after the transaction commits.

- [ ] **Step 4: Write failing busy, expired-lease, and retry tests**

```python
@pytest.mark.integration
async def test_active_processing_claim_returns_retryable_busy(database) -> None:
    message = await ingest_message(database)
    await install_processing_claim(database, message.event_id, FIXED_NOW + timedelta(seconds=30))
    result = await EventProcessor(database, 300).process(message, RecordingHandler(), FIXED_NOW)
    assert result.outcome == ProcessOutcome.BUSY


@pytest.mark.integration
async def test_expired_processing_claim_is_reclaimed(database) -> None:
    message = await ingest_message(database)
    await install_processing_claim(database, message.event_id, FIXED_NOW - timedelta(seconds=1))
    result = await EventProcessor(database, 300).process(message, RecordingHandler(), FIXED_NOW)
    assert result.outcome == ProcessOutcome.HANDLED
    assert (await load_processing(database, message.event_id)).attempt_count == 2


@pytest.mark.integration
async def test_handler_failure_is_recorded_and_next_delivery_can_succeed(database) -> None:
    message = await ingest_message(database)
    processor = EventProcessor(database, 300)
    with pytest.raises(RuntimeError, match="handler-token"):
        await processor.process(message, FailingHandler(), FIXED_NOW)
    failed = await load_processing(database, message.event_id)
    assert failed.stage == ProcessingStage.RECEIVED
    assert failed.claim_id is None
    assert failed.last_error_type == "RuntimeError"
    assert failed.last_error_summary == "operation failed"
    result = await processor.process(message, RecordingHandler(), FIXED_NOW)
    assert result.outcome == ProcessOutcome.HANDLED
    assert (await load_processing(database, message.event_id)).attempt_count == 2
```

- [ ] **Step 5: Implement handler dispatch and claim-protected finalization**

`_complete` and `_release` must constrain their UPDATE by `event_id`, `claim_id`, and `stage != HANDLED`. A zero row count raises `StaleClaimError` so an expired worker cannot overwrite a newer claim.

```python
async def process(
    self,
    message: EventAvailableMessage,
    handler: EventHandler,
    now: datetime | None = None,
) -> ProcessResult:
    claim = await self._claim(message, now or datetime.now(UTC))
    if isinstance(claim, ProcessResult):
        return claim
    try:
        # Handler code can call providers; the processing claim transaction is already committed.
        await handler.handle(claim.event)
    except Exception as error:
        await self._release(claim.event.id, claim.claim_id, error)
        raise
    await self._complete(claim.event.id, claim.claim_id)
    return ProcessResult(claim.event.id, ProcessOutcome.HANDLED)
```

Completion sets `HANDLED`, `processed_at`, clears claim/lease/error, and retains the incremented attempt count. Release retains the current stage and attempt count, clears claim/lease, and stores only `sanitize_error(error)`. Add structured logs for `event_id`, `user_id`, `workspace_id`, `claim_id`, and `outcome` without payloads.

- [ ] **Step 6: Run processor tests and commit**

Run: `uv run pytest tests/integration/events/test_processor.py -v && uv run ruff check src/eva_ai/events/processor.py tests/integration/events/test_processor.py && uv run mypy src/eva_ai/events/processor.py tests/integration/events/test_processor.py`

Expected: success, redelivery, unknown/scope rejection, busy claims, expired leases, failure history, and retry all pass.

```bash
git add src/eva_ai/events tests/integration/events/test_processor.py
git commit -m "feat: add idempotent event processor"
```

---

### Task 7: Worker Composition, Documentation, Full Verification, and Pull Request

**Files:**
- Create: `src/eva_ai/worker.py`
- Test: `tests/unit/test_worker.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `.github/workflows/ci.yml` only if the locked dependency or test command requires a mechanical update.

**Interfaces:**
- Consumes: `Settings`, `Database`, `InMemoryPublisher`, `GooglePubSubPublisher`, `OutboxRelay`, `EventProcessor`, `EventHandler`, and `EventAvailableMessage`.
- Produces: `build_publisher(settings, use_google)`, `build_outbox_relay(database, settings, publisher)`, `build_event_processor(database, settings)`, and `dispatch_event(processor, raw_message, handler)`; creates no server, endpoint, thread, subscription, or infinite loop.

- [ ] **Step 1: Write failing composition tests**

```python
def test_local_composition_uses_in_memory_publisher() -> None:
    settings = Settings(_env_file=None)
    publisher = build_publisher(settings, use_google=False)
    assert isinstance(publisher, InMemoryPublisher)


def test_google_composition_requires_project_id() -> None:
    settings = Settings(_env_file=None, pubsub_project_id=None)
    with pytest.raises(ValueError, match="EVA_PUBSUB_PROJECT_ID"):
        build_publisher(settings, use_google=True)


@pytest.mark.asyncio
async def test_dispatch_validates_raw_message_before_processing() -> None:
    processor = AsyncMock(spec=EventProcessor)
    handler = AsyncMock(spec=EventHandler)
    raw = EventAvailableMessage(
        outbox_message_id=uuid7(),
        event_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        event_type="test.created",
        schema_version=1,
    ).model_dump(mode="json")
    await dispatch_event(processor, raw, handler)
    processor.process.assert_awaited_once()
    assert processor.process.await_args.args[0] == EventAvailableMessage.model_validate(raw)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/unit/test_worker.py -v`

Expected: import fails because `eva_ai.worker` does not exist.

- [ ] **Step 3: Implement composition-only worker helpers**

```python
def build_publisher(settings: Settings, *, use_google: bool) -> Publisher:
    if not use_google:
        return InMemoryPublisher()
    if settings.pubsub_project_id is None:
        raise ValueError("EVA_PUBSUB_PROJECT_ID is required for Google Pub/Sub")
    return GooglePubSubPublisher(settings.pubsub_project_id)


def build_outbox_relay(
    database: Database, settings: Settings, publisher: Publisher
) -> OutboxRelay:
    return OutboxRelay(database, publisher, settings.outbox_lease_seconds)


def build_event_processor(database: Database, settings: Settings) -> EventProcessor:
    return EventProcessor(database, settings.processing_lease_seconds)


async def dispatch_event(
    processor: EventProcessor,
    raw_message: Mapping[str, object] | bytes | str,
    handler: EventHandler,
) -> ProcessResult:
    if isinstance(raw_message, bytes | str):
        message = EventAvailableMessage.model_validate_json(raw_message)
    else:
        message = EventAvailableMessage.model_validate(raw_message)
    return await processor.process(message, handler)
```

- [ ] **Step 4: Document configuration and operational boundaries**

Add these keys to `.env.example` without credential fields:

```dotenv
EVA_PUBSUB_PROJECT_ID=
EVA_PUBSUB_TOPIC_ID=eva-events
EVA_OUTBOX_BATCH_LIMIT=100
EVA_OUTBOX_LEASE_SECONDS=60
EVA_PROCESSING_LEASE_SECONDS=300
```

Add a `Milestone 1: Event backbone` README section showing:

```text
NewEvent -> PostgreSQL transaction [Event + EventProcessing + OutboxMessage]
         -> OutboxRelay claim -> Publisher acknowledgement
         -> EventProcessor claim -> EventHandler -> HANDLED
```

State that local tests use the in-memory publisher, Google Pub/Sub uses Application Default Credentials when selected, no GCP resources are created in this milestone, publication is at-least-once, and Telegram/Gmail behavior belongs to later milestones.

- [ ] **Step 5: Run the focused worker tests and all fast checks**

Run: `uv run pytest tests/unit/test_worker.py -v && uv run ruff format src migrations tests && uv run ruff check . && uv run mypy src migrations tests`

Expected: worker tests and all static checks pass.

- [ ] **Step 6: Prove a clean schema and run the complete suite**

Use a disposable, explicitly named Compose volume reset rather than deleting any broad path:

```bash
docker compose down -v
docker compose up -d --wait postgres
uv run alembic upgrade head
uv run alembic current
uv run pytest -v
make verify
```

Expected: Alembic reports `20260830_0002 (head)`, the full test suite passes, and `make verify` exits 0. The `docker compose down -v` command removes only this project's recoverable local PostgreSQL test data and must be announced before execution.

- [ ] **Step 7: Review the diff for scope and comments**

Run: `git status --short && git diff --check && git diff --stat main...HEAD && git diff main...HEAD`

Confirm all requirements in the approved design have a corresponding implementation/test, no payload or credential is logged/stored, no public endpoint/loop/GCP resource was added, and comments explain only non-obvious invariants.

- [ ] **Step 8: Commit the final composition and documentation**

```bash
git add src/eva_ai/worker.py tests/unit/test_worker.py .env.example README.md .github/workflows/ci.yml
git commit -m "docs: complete milestone 1 event backbone"
```

If `.github/workflows/ci.yml` was unchanged, omit it from `git add` rather than creating a no-op edit.

- [ ] **Step 9: Push the feature branch and open the pull request**

```bash
git push -u origin codex/milestone-1-event-backbone
gh pr create \
  --base main \
  --head codex/milestone-1-event-backbone \
  --title "Milestone 1: durable event backbone" \
  --body-file /tmp/eva-milestone-1-pr.md
```

Create `/tmp/eva-milestone-1-pr.md` with `apply_patch` before the command. Its body must summarize atomic ingestion, workspace/idempotency constraints, lease-based relay and processor behavior, Google adapter boundaries, deferred scope, and the exact verification commands/results.

- [ ] **Step 10: Verify remote CI and report the handoff**

Run: `gh pr checks --watch`

Expected: every required GitHub Actions check passes. Report the PR URL, branch, final commit SHA, local test count, migration head, and any intentional deferrals. Do not merge the pull request; the user requested review through a PR.
