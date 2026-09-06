from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pgvector.sqlalchemy import Vector
from pydantic import JsonValue
from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from eva_ai.db.base import Base
from eva_ai.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin
from eva_ai.memory.types import (
    EpisodicMemoryStatus,
    MemoryEpisodeType,
    MemoryFactStatus,
    MemoryScopeType,
    MemorySourceType,
)

MEMORY_FACT_SCOPE_REFERENCE = [
    "memory_facts.id",
    "memory_facts.workspace_id",
    "memory_facts.user_id",
]
EPISODE_SCOPE_REFERENCE = [
    "episodic_memories.id",
    "episodic_memories.workspace_id",
    "episodic_memories.user_id",
]


class MemoryFact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memory_facts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_memory_facts_workspace_user",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["goal_id", "workspace_id", "user_id"],
            ["goals.id", "goals.workspace_id", "goals.user_id"],
            name="fk_memory_facts_goal_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_memory_facts_situation_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["supersedes_memory_id", "workspace_id", "user_id"],
            MEMORY_FACT_SCOPE_REFERENCE,
            name="fk_memory_facts_supersedes_scope",
        ),
        UniqueConstraint("id", "workspace_id", "user_id", name="uq_memory_facts_id_scope"),
        UniqueConstraint(
            "workspace_id",
            "user_id",
            "idempotency_key",
            name="uq_memory_facts_scope_idempotency",
        ),
        CheckConstraint(
            "scope_type IN ('WORKSPACE', 'GOAL', 'SITUATION')",
            name="ck_memory_facts_scope_type",
        ),
        CheckConstraint(
            "(scope_type = 'WORKSPACE' AND goal_id IS NULL AND situation_id IS NULL) OR "
            "(scope_type = 'GOAL' AND goal_id IS NOT NULL AND situation_id IS NULL) OR "
            "(scope_type = 'SITUATION' AND goal_id IS NULL AND situation_id IS NOT NULL)",
            name="ck_memory_facts_scope_target",
        ),
        CheckConstraint(
            "source_type IN ('USER_EXPLICIT', 'USER_BEHAVIOR', 'AGENT_INFERRED', "
            "'EXTERNAL_EVENT', 'SYSTEM_OBSERVED')",
            name="ck_memory_facts_source_type",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'SUPERSEDED', 'RETRACTED')",
            name="ck_memory_facts_status",
        ),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_memory_facts_confidence"),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from", name="ck_memory_facts_validity"
        ),
        CheckConstraint(
            "supersedes_memory_id IS NULL OR supersedes_memory_id <> id",
            name="ck_memory_facts_not_self_superseding",
        ),
        CheckConstraint("btrim(namespace) <> ''", name="ck_memory_facts_namespace_nonblank"),
        CheckConstraint("btrim(key) <> ''", name="ck_memory_facts_key_nonblank"),
        CheckConstraint("btrim(source_ref) <> ''", name="ck_memory_facts_source_ref_nonblank"),
        Index(
            "uq_memory_facts_active_workspace_slot",
            "workspace_id",
            "user_id",
            "namespace",
            "key",
            unique=True,
            postgresql_where=text("status = 'ACTIVE' AND scope_type = 'WORKSPACE'"),
        ),
        Index(
            "uq_memory_facts_active_goal_slot",
            "workspace_id",
            "user_id",
            "goal_id",
            "namespace",
            "key",
            unique=True,
            postgresql_where=text("status = 'ACTIVE' AND scope_type = 'GOAL'"),
        ),
        Index(
            "uq_memory_facts_active_situation_slot",
            "workspace_id",
            "user_id",
            "situation_id",
            "namespace",
            "key",
            unique=True,
            postgresql_where=text("status = 'ACTIVE' AND scope_type = 'SITUATION'"),
        ),
        Index("ix_memory_facts_scope_status", "workspace_id", "user_id", "status"),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    namespace: Mapped[str] = mapped_column(String(100))
    key: Mapped[str] = mapped_column(String(200))
    # SQL JSON null is valid memory content, while the database column itself must always exist.
    value_json: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    scope_type: Mapped[MemoryScopeType] = mapped_column(String(32))
    goal_id: Mapped[UUID | None]
    situation_id: Mapped[UUID | None]
    source_type: Mapped[MemorySourceType] = mapped_column(String(32))
    source_ref: Mapped[str] = mapped_column(String(500))
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    idempotency_key: Mapped[str] = mapped_column(String(500))
    status: Mapped[MemoryFactStatus] = mapped_column(String(32), server_default="ACTIVE")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_memory_id: Mapped[UUID | None]


class EpisodicMemory(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "episodic_memories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_episodic_memories_workspace_user",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_episodic_memories_situation_scope",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "workspace_id", "user_id", name="uq_episodic_memories_id_scope"),
        UniqueConstraint(
            "workspace_id",
            "user_id",
            "idempotency_key",
            name="uq_episodic_memories_scope_idempotency",
        ),
        CheckConstraint(
            "type IN ('DECISION', 'EXPERIENCE', 'OUTCOME', 'SITUATION_SUMMARY')",
            name="ck_episodic_memories_type",
        ),
        CheckConstraint(
            "source_type IN ('USER_EXPLICIT', 'USER_BEHAVIOR', 'AGENT_INFERRED', "
            "'EXTERNAL_EVENT', 'SYSTEM_OBSERVED')",
            name="ck_episodic_memories_source_type",
        ),
        CheckConstraint("status IN ('ACTIVE', 'RETRACTED')", name="ck_episodic_memories_status"),
        CheckConstraint("importance BETWEEN 0 AND 1", name="ck_episodic_memories_importance"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_episodic_memories_confidence"),
        CheckConstraint("cardinality(entities) <= 20", name="ck_episodic_memories_entity_count"),
        CheckConstraint("embedding_dimensions = 1536", name="ck_episodic_memories_dimensions"),
        CheckConstraint(
            "char_length(embedding_input_digest) = 64", name="ck_episodic_memories_digest"
        ),
        CheckConstraint("btrim(summary) <> ''", name="ck_episodic_memories_summary_nonblank"),
        CheckConstraint("btrim(source_ref) <> ''", name="ck_episodic_memories_source_ref_nonblank"),
        Index("ix_episodic_memories_scope_status", "workspace_id", "user_id", "status"),
        Index("ix_episodic_memories_scope_occurred", "workspace_id", "user_id", "occurred_at"),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    type: Mapped[MemoryEpisodeType] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text)
    entities: Mapped[list[str]] = mapped_column(ARRAY(String(200)))
    situation_id: Mapped[UUID | None]
    importance: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    source_type: Mapped[MemorySourceType] = mapped_column(String(32))
    source_ref: Mapped[str] = mapped_column(String(500))
    idempotency_key: Mapped[str] = mapped_column(String(500))
    status: Mapped[EpisodicMemoryStatus] = mapped_column(String(32), server_default="ACTIVE")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    embedding: Mapped[list[float]] = mapped_column(Vector(1536))
    embedding_model: Mapped[str] = mapped_column(String(100))
    embedding_dimensions: Mapped[int] = mapped_column(Integer)
    embedding_input_digest: Mapped[str] = mapped_column(String(64))
    embedded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class EpisodicMemoryGoal(Base):
    __tablename__ = "episodic_memory_goals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["memory_id", "workspace_id", "user_id"],
            EPISODE_SCOPE_REFERENCE,
            name="fk_episodic_memory_goals_memory_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["goal_id", "workspace_id", "user_id"],
            ["goals.id", "goals.workspace_id", "goals.user_id"],
            name="fk_episodic_memory_goals_goal_scope",
            ondelete="CASCADE",
        ),
        Index("ix_episodic_memory_goals_scope_goal", "workspace_id", "user_id", "goal_id"),
    )

    memory_id: Mapped[UUID] = mapped_column(primary_key=True)
    goal_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID]
    user_id: Mapped[UUID]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
