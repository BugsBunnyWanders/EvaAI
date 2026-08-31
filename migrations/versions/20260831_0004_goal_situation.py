"""Add Goal and Situation domain storage.

Revision ID: 20260831_0004
Revises: 20260830_0003
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260831_0004"
down_revision: str | None = "20260830_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_events_id_workspace_user",
        "events",
        ["id", "workspace_id", "user_id"],
    )
    op.create_table(
        "goals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(length=100), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="50", nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "success_criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "constraints",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "autonomy_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text('\'{"mode": "REQUIRE_APPROVAL"}\'::jsonb'),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("parent_goal_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.CheckConstraint("mode IN ('ACHIEVE', 'MAINTAIN')", name="ck_goals_mode"),
        sa.CheckConstraint("priority BETWEEN 0 AND 100", name="ck_goals_priority"),
        sa.CheckConstraint(
            "status IN ('CANDIDATE', 'ACTIVE', 'PAUSED', 'COMPLETED', 'ABANDONED')",
            name="ck_goals_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(success_criteria) = 'array' "
            "AND jsonb_array_length(success_criteria) <= 20",
            name="ck_goals_success_criteria",
        ),
        sa.CheckConstraint("jsonb_typeof(constraints) = 'object'", name="ck_goals_constraints"),
        sa.CheckConstraint(
            'autonomy_policy = \'{"mode": "REQUIRE_APPROVAL"}\'::jsonb',
            name="ck_goals_autonomy_policy",
        ),
        sa.CheckConstraint("source IN ('USER_EXPLICIT', 'AGENT_INFERRED')", name="ck_goals_source"),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1", name="ck_goals_confidence"
        ),
        sa.CheckConstraint("btrim(title) <> ''", name="ck_goals_title_nonblank"),
        sa.CheckConstraint("btrim(objective) <> ''", name="ck_goals_objective_nonblank"),
        sa.CheckConstraint("btrim(domain) <> ''", name="ck_goals_domain_nonblank"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_goals_workspace_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", "user_id", name="uq_goals_id_workspace_user"),
    )
    op.create_foreign_key(
        "fk_goals_parent_scope",
        "goals",
        "goals",
        ["parent_goal_id", "workspace_id", "user_id"],
        ["id", "workspace_id", "user_id"],
    )
    op.create_index("ix_goals_scope_status", "goals", ["workspace_id", "user_id", "status"])
    op.create_index(
        "ix_goals_scope_priority_created",
        "goals",
        ["workspace_id", "user_id", "priority", "created_at"],
    )

    op.create_table(
        "situations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), server_default="OPEN", nullable=False),
        sa.Column("attention", sa.String(length=20), server_default="NORMAL", nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("current_state", sa.String(length=100), nullable=False),
        sa.Column("next_action", sa.String(length=1000), nullable=True),
        sa.Column("next_expected", sa.String(length=1000), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.CheckConstraint("type IN ('EMAIL_THREAD')", name="ck_situations_type"),
        sa.CheckConstraint(
            "lifecycle IN ('OPEN', 'ACTIVE', 'WAITING_USER', 'WAITING_EXTERNAL', "
            "'RESOLVED', 'ABANDONED')",
            name="ck_situations_lifecycle",
        ),
        sa.CheckConstraint(
            "attention IN ('LOW', 'NORMAL', 'HIGH', 'URGENT')",
            name="ck_situations_attention",
        ),
        sa.CheckConstraint("version >= 1", name="ck_situations_version"),
        sa.CheckConstraint("btrim(title) <> ''", name="ck_situations_title_nonblank"),
        sa.CheckConstraint(
            "btrim(current_state) <> ''", name="ck_situations_current_state_nonblank"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_situations_workspace_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", "user_id", name="uq_situations_id_workspace_user"
        ),
    )
    op.create_index(
        "ix_situations_scope_lifecycle",
        "situations",
        ["workspace_id", "user_id", "lifecycle"],
    )
    op.create_index(
        "ix_situations_scope_last_activity",
        "situations",
        ["workspace_id", "user_id", "last_activity_at"],
    )

    op.create_table(
        "situation_correlation_keys",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_key", sa.String(length=500), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("situation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('GMAIL_THREAD')", name="ck_situation_correlation_keys_kind"),
        sa.CheckConstraint(
            "btrim(correlation_key) <> ''", name="ck_situation_correlation_keys_key_nonblank"
        ),
        sa.ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_situation_correlation_keys_situation_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_situation_correlation_keys_workspace_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "correlation_key"),
    )
    op.create_index(
        "ix_situation_correlation_keys_scope_situation",
        "situation_correlation_keys",
        ["workspace_id", "user_id", "situation_id"],
    )

    op.create_table(
        "situation_events",
        sa.Column("situation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_method", sa.String(length=32), nullable=False),
        sa.Column("correlation_key", sa.String(length=500), nullable=True),
        sa.Column(
            "linked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "correlation_method IN ('DETERMINISTIC_KEY', 'EXPLICIT')",
            name="ck_situation_events_method",
        ),
        sa.ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_situation_events_situation_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_situation_events_event_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("situation_id", "event_id"),
    )
    op.create_index(
        "ix_situation_events_scope_event",
        "situation_events",
        ["workspace_id", "user_id", "event_id"],
    )

    op.create_table(
        "situation_goals",
        sa.Column("situation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("goal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relevance", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("contribution", sa.String(length=20), nullable=False),
        sa.Column("reasoning", sa.String(length=1000), nullable=True),
        sa.Column(
            "linked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("relevance BETWEEN 0 AND 1", name="ck_situation_goals_relevance"),
        sa.CheckConstraint(
            "contribution IN ('SUPPORTS', 'BLOCKS', 'CONTEXT')",
            name="ck_situation_goals_contribution",
        ),
        sa.ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_situation_goals_situation_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["goal_id", "workspace_id", "user_id"],
            ["goals.id", "goals.workspace_id", "goals.user_id"],
            name="fk_situation_goals_goal_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("situation_id", "goal_id"),
    )
    op.create_index(
        "ix_situation_goals_scope_goal",
        "situation_goals",
        ["workspace_id", "user_id", "goal_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_situation_goals_scope_goal", table_name="situation_goals")
    op.drop_table("situation_goals")
    op.drop_index("ix_situation_events_scope_event", table_name="situation_events")
    op.drop_table("situation_events")
    op.drop_index(
        "ix_situation_correlation_keys_scope_situation",
        table_name="situation_correlation_keys",
    )
    op.drop_table("situation_correlation_keys")
    op.drop_index("ix_situations_scope_last_activity", table_name="situations")
    op.drop_index("ix_situations_scope_lifecycle", table_name="situations")
    op.drop_table("situations")
    op.drop_index("ix_goals_scope_priority_created", table_name="goals")
    op.drop_index("ix_goals_scope_status", table_name="goals")
    op.drop_table("goals")
    op.drop_constraint("uq_events_id_workspace_user", "events", type_="unique")
