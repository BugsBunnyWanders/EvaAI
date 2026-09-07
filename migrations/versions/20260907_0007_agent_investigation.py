"""Add durable agent investigation runs.

Revision ID: 20260907_0007
Revises: 20260906_0006
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260907_0007"
down_revision: str | None = "20260906_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("situation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="QUEUED", nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("agent_version", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("output_schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_digest", sa.String(length=64), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("provider_response_id", sa.String(length=500), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "tool_audit",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.String(length=500), nullable=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'RETRYABLE_FAILURE', "
            "'PERMANENT_FAILURE')",
            name="ck_agent_runs_status",
        ),
        sa.CheckConstraint("output_schema_version > 0", name="ck_agent_runs_schema_version"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_agent_runs_attempt_count"),
        sa.CheckConstraint(
            "input_digest IS NULL OR char_length(input_digest) = 64",
            name="ck_agent_runs_input_digest",
        ),
        sa.CheckConstraint("input_tokens >= 0", name="ck_agent_runs_input_tokens"),
        sa.CheckConstraint("output_tokens >= 0", name="ck_agent_runs_output_tokens"),
        sa.CheckConstraint("total_tokens >= 0", name="ck_agent_runs_total_tokens"),
        sa.ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_agent_runs_event_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["signal_id", "workspace_id", "user_id", "event_id"],
            ["signals.id", "signals.workspace_id", "signals.user_id", "signals.event_id"],
            name="fk_agent_runs_signal_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_agent_runs_situation_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", "user_id", name="uq_agent_runs_id_scope"),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            "signal_id",
            "agent_version",
            name="uq_agent_runs_signal_version",
        ),
    )
    op.create_index(
        "ix_agent_runs_scope_status_retry",
        "agent_runs",
        ["workspace_id", "user_id", "status", "next_retry_at"],
    )
    op.create_index("ix_agent_runs_situation_queued", "agent_runs", ["situation_id", "queued_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_situation_queued", table_name="agent_runs")
    op.drop_index("ix_agent_runs_scope_status_retry", table_name="agent_runs")
    op.drop_table("agent_runs")
