"""Add relevance Signal and evaluation-attempt storage.

Revision ID: 20260901_0005
Revises: 20260831_0004
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260901_0005"
down_revision: str | None = "20260831_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "relevance_evaluation_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evaluation_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("operator_reason", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("classifier_version", sa.String(length=100), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_number > 0", name="ck_relevance_attempt_number"),
        sa.CheckConstraint(
            "trigger IN ('INITIAL', 'EXPLICIT_REEVALUATION', 'BACKFILL')",
            name="ck_relevance_attempt_trigger",
        ),
        sa.CheckConstraint(
            "status IN ('STARTED', 'SUCCEEDED', 'RETRYABLE_FAILURE', 'PERMANENT_FAILURE')",
            name="ck_relevance_attempt_status",
        ),
        sa.CheckConstraint("char_length(input_digest) = 64", name="ck_relevance_attempt_digest"),
        sa.CheckConstraint(
            "(status = 'STARTED' AND completed_at IS NULL AND failure_code IS NULL) OR "
            "(status = 'SUCCEEDED' AND completed_at IS NOT NULL AND failure_code IS NULL) OR "
            "(status IN ('RETRYABLE_FAILURE', 'PERMANENT_FAILURE') "
            "AND completed_at IS NOT NULL AND failure_code IS NOT NULL)",
            name="ck_relevance_attempt_completion",
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_relevance_attempts_event_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "workspace_id",
            "user_id",
            "event_id",
            name="uq_relevance_attempts_id_scope",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            "event_id",
            "evaluation_key",
            "attempt_number",
            name="uq_relevance_attempt_scope",
        ),
    )
    op.create_index(
        "ix_relevance_attempts_scope_event",
        "relevance_evaluation_attempts",
        ["workspace_id", "user_id", "event_id", "started_at"],
    )

    op.create_table(
        "signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("producer", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("classifier_version", sa.String(length=100), nullable=False),
        sa.Column("policy_version", sa.String(length=100), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("operator_reason", sa.String(length=500), nullable=True),
        sa.Column("evaluation_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("successful_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("situation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("supersedes_signal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_current", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('RELEVANCE')", name="ck_signals_kind"),
        sa.CheckConstraint("schema_version > 0", name="ck_signals_schema_version"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_signals_payload_object"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_signals_confidence"),
        sa.CheckConstraint(
            "disposition IN ('IGNORE', 'RECORD', 'NOTIFY', 'INVESTIGATE')",
            name="ck_signals_disposition",
        ),
        sa.CheckConstraint("producer IN ('DETERMINISTIC', 'AI')", name="ck_signals_producer"),
        sa.CheckConstraint(
            "trigger IN ('INITIAL', 'EXPLICIT_REEVALUATION', 'BACKFILL')",
            name="ck_signals_trigger",
        ),
        sa.CheckConstraint("char_length(input_digest) = 64", name="ck_signals_digest"),
        sa.CheckConstraint(
            "supersedes_signal_id IS NULL OR supersedes_signal_id <> id",
            name="ck_signals_not_self_superseding",
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_signals_event_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["successful_attempt_id", "workspace_id", "user_id", "event_id"],
            [
                "relevance_evaluation_attempts.id",
                "relevance_evaluation_attempts.workspace_id",
                "relevance_evaluation_attempts.user_id",
                "relevance_evaluation_attempts.event_id",
            ],
            name="fk_signals_attempt_scope",
        ),
        sa.ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_signals_situation_scope",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_signal_id", "workspace_id", "user_id", "event_id"],
            ["signals.id", "signals.workspace_id", "signals.user_id", "signals.event_id"],
            name="fk_signals_supersedes_scope",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", "user_id", name="uq_signals_id_workspace_user"),
        sa.UniqueConstraint(
            "id", "workspace_id", "user_id", "event_id", name="uq_signals_id_scope_event"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            "event_id",
            "evaluation_key",
            name="uq_signals_scope_evaluation",
        ),
    )
    op.create_index(
        "uq_signals_current_relevance",
        "signals",
        ["workspace_id", "user_id", "event_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'RELEVANCE' AND is_current"),
    )
    op.create_index(
        "ix_signals_scope_event_created",
        "signals",
        ["workspace_id", "user_id", "event_id", "created_at"],
    )

    op.create_table(
        "signal_goals",
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("goal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relevance", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("contribution", sa.String(length=20), nullable=False),
        sa.Column("reasoning", sa.String(length=500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("relevance BETWEEN 0 AND 1", name="ck_signal_goals_relevance"),
        sa.CheckConstraint(
            "contribution IN ('SUPPORTS', 'BLOCKS', 'CONTEXT')",
            name="ck_signal_goals_contribution",
        ),
        sa.ForeignKeyConstraint(
            ["signal_id", "workspace_id", "user_id"],
            ["signals.id", "signals.workspace_id", "signals.user_id"],
            name="fk_signal_goals_signal_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["goal_id", "workspace_id", "user_id"],
            ["goals.id", "goals.workspace_id", "goals.user_id"],
            name="fk_signal_goals_goal_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("signal_id", "goal_id"),
    )
    op.create_index(
        "ix_signal_goals_scope_goal",
        "signal_goals",
        ["workspace_id", "user_id", "goal_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_signal_goals_scope_goal", table_name="signal_goals")
    op.drop_table("signal_goals")
    op.drop_index("ix_signals_scope_event_created", table_name="signals")
    op.drop_index("uq_signals_current_relevance", table_name="signals")
    op.drop_table("signals")
    op.drop_index("ix_relevance_attempts_scope_event", table_name="relevance_evaluation_attempts")
    op.drop_table("relevance_evaluation_attempts")
