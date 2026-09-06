"""Add structured and episodic memory storage.

Revision ID: 20260906_0006
Revises: 20260901_0005
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20260906_0006"
down_revision: str | None = "20260901_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_facts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("namespace", sa.String(length=100), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("goal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("situation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=500), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("idempotency_key", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope_type IN ('WORKSPACE', 'GOAL', 'SITUATION')", name="ck_memory_facts_scope_type"
        ),
        sa.CheckConstraint(
            "(scope_type = 'WORKSPACE' AND goal_id IS NULL AND situation_id IS NULL) OR "
            "(scope_type = 'GOAL' AND goal_id IS NOT NULL AND situation_id IS NULL) OR "
            "(scope_type = 'SITUATION' AND goal_id IS NULL AND situation_id IS NOT NULL)",
            name="ck_memory_facts_scope_target",
        ),
        sa.CheckConstraint(
            "source_type IN ('USER_EXPLICIT', 'USER_BEHAVIOR', 'AGENT_INFERRED', "
            "'EXTERNAL_EVENT', 'SYSTEM_OBSERVED')",
            name="ck_memory_facts_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUPERSEDED', 'RETRACTED')", name="ck_memory_facts_status"
        ),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_memory_facts_confidence"),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from", name="ck_memory_facts_validity"
        ),
        sa.CheckConstraint(
            "supersedes_memory_id IS NULL OR supersedes_memory_id <> id",
            name="ck_memory_facts_not_self_superseding",
        ),
        sa.CheckConstraint("btrim(namespace) <> ''", name="ck_memory_facts_namespace_nonblank"),
        sa.CheckConstraint("btrim(key) <> ''", name="ck_memory_facts_key_nonblank"),
        sa.CheckConstraint("btrim(source_ref) <> ''", name="ck_memory_facts_source_ref_nonblank"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_memory_facts_workspace_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["goal_id", "workspace_id", "user_id"],
            ["goals.id", "goals.workspace_id", "goals.user_id"],
            name="fk_memory_facts_goal_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_memory_facts_situation_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_memory_id", "workspace_id", "user_id"],
            ["memory_facts.id", "memory_facts.workspace_id", "memory_facts.user_id"],
            name="fk_memory_facts_supersedes_scope",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", "user_id", name="uq_memory_facts_id_scope"),
        sa.UniqueConstraint(
            "workspace_id", "user_id", "idempotency_key", name="uq_memory_facts_scope_idempotency"
        ),
    )
    op.create_index(
        "uq_memory_facts_active_workspace_slot",
        "memory_facts",
        ["workspace_id", "user_id", "namespace", "key"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE' AND scope_type = 'WORKSPACE'"),
    )
    op.create_index(
        "uq_memory_facts_active_goal_slot",
        "memory_facts",
        ["workspace_id", "user_id", "goal_id", "namespace", "key"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE' AND scope_type = 'GOAL'"),
    )
    op.create_index(
        "uq_memory_facts_active_situation_slot",
        "memory_facts",
        ["workspace_id", "user_id", "situation_id", "namespace", "key"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE' AND scope_type = 'SITUATION'"),
    )
    op.create_index(
        "ix_memory_facts_scope_status", "memory_facts", ["workspace_id", "user_id", "status"]
    )

    op.create_table(
        "episodic_memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("entities", postgresql.ARRAY(sa.String(length=200)), nullable=False),
        sa.Column("situation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("importance", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=500), nullable=False),
        sa.Column("idempotency_key", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("embedding", Vector(dim=1536), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding_input_digest", sa.String(length=64), nullable=False),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "type IN ('DECISION', 'EXPERIENCE', 'OUTCOME', 'SITUATION_SUMMARY')",
            name="ck_episodic_memories_type",
        ),
        sa.CheckConstraint(
            "source_type IN ('USER_EXPLICIT', 'USER_BEHAVIOR', 'AGENT_INFERRED', "
            "'EXTERNAL_EVENT', 'SYSTEM_OBSERVED')",
            name="ck_episodic_memories_source_type",
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'RETRACTED')", name="ck_episodic_memories_status"),
        sa.CheckConstraint("importance BETWEEN 0 AND 1", name="ck_episodic_memories_importance"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_episodic_memories_confidence"),
        sa.CheckConstraint("cardinality(entities) <= 20", name="ck_episodic_memories_entity_count"),
        sa.CheckConstraint("embedding_dimensions = 1536", name="ck_episodic_memories_dimensions"),
        sa.CheckConstraint(
            "char_length(embedding_input_digest) = 64", name="ck_episodic_memories_digest"
        ),
        sa.CheckConstraint("btrim(summary) <> ''", name="ck_episodic_memories_summary_nonblank"),
        sa.CheckConstraint(
            "btrim(source_ref) <> ''", name="ck_episodic_memories_source_ref_nonblank"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_episodic_memories_workspace_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_episodic_memories_situation_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", "user_id", name="uq_episodic_memories_id_scope"),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            "idempotency_key",
            name="uq_episodic_memories_scope_idempotency",
        ),
    )
    op.create_index(
        "ix_episodic_memories_scope_status",
        "episodic_memories",
        ["workspace_id", "user_id", "status"],
    )
    op.create_index(
        "ix_episodic_memories_scope_occurred",
        "episodic_memories",
        ["workspace_id", "user_id", "occurred_at"],
    )

    op.create_table(
        "episodic_memory_goals",
        sa.Column("memory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("goal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["memory_id", "workspace_id", "user_id"],
            ["episodic_memories.id", "episodic_memories.workspace_id", "episodic_memories.user_id"],
            name="fk_episodic_memory_goals_memory_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["goal_id", "workspace_id", "user_id"],
            ["goals.id", "goals.workspace_id", "goals.user_id"],
            name="fk_episodic_memory_goals_goal_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("memory_id", "goal_id"),
    )
    op.create_index(
        "ix_episodic_memory_goals_scope_goal",
        "episodic_memory_goals",
        ["workspace_id", "user_id", "goal_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_episodic_memory_goals_scope_goal", table_name="episodic_memory_goals")
    op.drop_table("episodic_memory_goals")
    op.drop_index("ix_episodic_memories_scope_occurred", table_name="episodic_memories")
    op.drop_index("ix_episodic_memories_scope_status", table_name="episodic_memories")
    op.drop_table("episodic_memories")
    op.drop_index("ix_memory_facts_scope_status", table_name="memory_facts")
    op.drop_index("uq_memory_facts_active_situation_slot", table_name="memory_facts")
    op.drop_index("uq_memory_facts_active_goal_slot", table_name="memory_facts")
    op.drop_index("uq_memory_facts_active_workspace_slot", table_name="memory_facts")
    op.drop_table("memory_facts")
