from datetime import datetime
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from eva_ai.db.base import Base
from eva_ai.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin
from eva_ai.events.types import OutboxState, PrincipalType, ProcessingStage

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
        UniqueConstraint("id", "workspace_id", "user_id", name="uq_events_id_workspace_user"),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_events_workspace_idempotency"),
        CheckConstraint("schema_version > 0", name="ck_events_schema_version_positive"),
        CheckConstraint(
            "principal_type IN ('USER', 'SYSTEM', 'EXTERNAL')",
            name="ck_events_principal_type",
        ),
        Index("ix_events_workspace_occurred", "workspace_id", "occurred_at"),
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
        Index("ix_event_processing_stage_retry", "stage", "next_retry_at"),
    )

    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    stage: Mapped[ProcessingStage] = mapped_column(String(32), server_default="RECEIVED")
    attempt_count: Mapped[int] = mapped_column(default=0, server_default="0")
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
        CheckConstraint("state IN ('PENDING', 'PUBLISHING', 'PUBLISHED')", name="ck_outbox_state"),
        Index("ix_outbox_state_available", "state", "available_at"),
    )

    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    destination: Mapped[str] = mapped_column(String(200))
    message_type: Mapped[str] = mapped_column(String(200))
    schema_version: Mapped[int]
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    state: Mapped[OutboxState] = mapped_column(String(32), server_default="PENDING")
    attempt_count: Mapped[int] = mapped_column(default=0, server_default="0")
    last_error_type: Mapped[str | None] = mapped_column(String(200))
    last_error_summary: Mapped[str | None] = mapped_column(String(500))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claim_id: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
