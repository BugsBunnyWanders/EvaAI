"""Add immutable action proposals, approvals, execution, and managed Gmail drafts.

Revision ID: 20260917_0010
Revises: 20260907_0009
Create Date: 2026-09-17
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260917_0010"
down_revision: str | None = "20260907_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _scope_columns() -> tuple[sa.Column[Any], sa.Column[Any]]:
    return (
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
    )


def upgrade() -> None:
    # These composite candidate keys let every action relationship prove tenant scope in the
    # database instead of trusting application filters alone.
    op.create_unique_constraint(
        "uq_connector_accounts_id_scope",
        "connector_accounts",
        ["id", "workspace_id", "user_id"],
    )
    op.create_unique_constraint(
        "uq_conversation_turns_id_scope",
        "conversation_turns",
        ["id", "workspace_id", "user_id"],
    )
    op.add_column(
        "notifications",
        sa.Column("reply_markup", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.create_table(
        "action_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        *_scope_columns(),
        sa.Column("connector_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("situation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("goal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("supersedes_proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_key", sa.String(length=500), nullable=False),
        sa.Column("proposal_family_key", sa.String(length=500), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("capability", sa.String(length=100), nullable=False),
        sa.Column(
            "parameters_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("parameters_hash", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.String(length=20), nullable=False),
        sa.Column("policy_decision", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "capability IN ('gmail.create_draft', 'gmail.update_draft', "
            "'gmail.send_draft', 'gmail.delete_draft')",
            name="ck_action_proposals_capability",
        ),
        sa.CheckConstraint(
            "status IN ('PROPOSED', 'QUEUED', 'WAITING_APPROVAL', 'APPROVED', 'REJECTED', "
            "'EXPIRED', 'SUPERSEDED', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name="ck_action_proposals_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_action_proposals_version"),
        sa.CheckConstraint("char_length(parameters_hash) = 64", name="ck_action_proposals_hash"),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name="ck_action_proposals_expiry",
        ),
        sa.CheckConstraint("btrim(description) <> ''", name="ck_action_proposals_description"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_action_proposals_workspace_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connector_account_id", "workspace_id", "user_id"],
            [
                "connector_accounts.id",
                "connector_accounts.workspace_id",
                "connector_accounts.user_id",
            ],
            name="fk_action_proposals_connector_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_action_proposals_event_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_action_proposals_situation_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["goal_id", "workspace_id", "user_id"],
            ["goals.id", "goals.workspace_id", "goals.user_id"],
            name="fk_action_proposals_goal_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id", "workspace_id", "user_id"],
            ["agent_runs.id", "agent_runs.workspace_id", "agent_runs.user_id"],
            name="fk_action_proposals_agent_run_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_turn_id", "workspace_id", "user_id"],
            [
                "conversation_turns.id",
                "conversation_turns.workspace_id",
                "conversation_turns.user_id",
            ],
            name="fk_action_proposals_conversation_turn_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_proposal_id", "workspace_id", "user_id"],
            ["action_proposals.id", "action_proposals.workspace_id", "action_proposals.user_id"],
            name="fk_action_proposals_supersedes_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", "user_id", name="uq_action_proposals_id_scope"),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            "source_key",
            name="uq_action_proposals_scope_source_key",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            "proposal_family_key",
            "version",
            name="uq_action_proposals_scope_version",
        ),
    )
    op.create_index(
        "ix_action_proposals_scope_status",
        "action_proposals",
        ["workspace_id", "user_id", "status", "created_at"],
    )

    op.create_table(
        "action_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        *_scope_columns(),
        sa.Column("parameters_hash", sa.String(length=64), nullable=False),
        sa.Column("principal_type", sa.String(length=32), nullable=False),
        sa.Column("telegram_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("callback_token_digest", sa.String(length=64), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'GRANTED', 'REJECTED', 'EXPIRED', 'SUPERSEDED')",
            name="ck_action_approvals_status",
        ),
        sa.CheckConstraint("char_length(parameters_hash) = 64", name="ck_action_approvals_hash"),
        sa.CheckConstraint(
            "char_length(callback_token_digest) = 64", name="ck_action_approvals_token"
        ),
        sa.CheckConstraint("expires_at > requested_at", name="ck_action_approvals_expiry"),
        sa.ForeignKeyConstraint(
            ["proposal_id", "workspace_id", "user_id"],
            ["action_proposals.id", "action_proposals.workspace_id", "action_proposals.user_id"],
            name="fk_action_approvals_proposal_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["telegram_account_id", "workspace_id", "user_id"],
            ["telegram_accounts.id", "telegram_accounts.workspace_id", "telegram_accounts.user_id"],
            name="fk_action_approvals_telegram_account_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", "user_id", name="uq_action_approvals_id_scope"),
        sa.UniqueConstraint("proposal_id", name="uq_action_approvals_proposal"),
        sa.UniqueConstraint("callback_token_digest", name="uq_action_approvals_callback_digest"),
    )
    op.create_index(
        "ix_action_approvals_scope_status_expiry",
        "action_approvals",
        ["workspace_id", "user_id", "status", "expires_at"],
    )

    op.create_table(
        "actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        *_scope_columns(),
        sa.Column("capability", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=500), nullable=False),
        sa.Column("cloud_task_name", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="QUEUED", nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("provider_call_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "capability IN ('gmail.create_draft', 'gmail.update_draft', "
            "'gmail.send_draft', 'gmail.delete_draft')",
            name="ck_actions_capability",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN')",
            name="ck_actions_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_actions_attempt_count"),
        sa.CheckConstraint(
            "provider_call_started_at IS NULL OR started_at IS NOT NULL",
            name="ck_actions_provider_boundary",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NOT NULL",
            name="ck_actions_finished_after_start",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id", "workspace_id", "user_id"],
            ["action_proposals.id", "action_proposals.workspace_id", "action_proposals.user_id"],
            name="fk_actions_proposal_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_actions_event_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", "user_id", name="uq_actions_id_scope"),
        sa.UniqueConstraint("proposal_id", name="uq_actions_proposal"),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            "idempotency_key",
            name="uq_actions_scope_idempotency",
        ),
    )
    op.create_index(
        "ix_actions_scope_status_lease",
        "actions",
        ["workspace_id", "user_id", "status", "lease_expires_at"],
    )

    op.create_table(
        "action_results",
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=False),
        *_scope_columns(),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("provider_status_category", sa.String(length=100), nullable=True),
        sa.Column("provider_draft_id", sa.String(length=500), nullable=True),
        sa.Column("provider_message_id", sa.String(length=500), nullable=True),
        sa.Column("provider_thread_id", sa.String(length=500), nullable=True),
        sa.Column(
            "result_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(outcome) <> ''", name="ck_action_results_outcome"),
        sa.ForeignKeyConstraint(
            ["action_id", "workspace_id", "user_id"],
            ["actions.id", "actions.workspace_id", "actions.user_id"],
            name="fk_action_results_action_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("action_id"),
    )

    op.create_table(
        "managed_gmail_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        *_scope_columns(),
        sa.Column("connector_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_draft_id", sa.String(length=500), nullable=False),
        sa.Column("provider_message_id", sa.String(length=500), nullable=True),
        sa.Column("gmail_thread_id", sa.String(length=500), nullable=True),
        sa.Column("create_proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("active_send_proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("send_proposal_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("current_content_hash", sa.String(length=64), nullable=False),
        sa.Column("rfc_message_id", sa.String(length=998), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
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
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('CREATING', 'READY', 'UPDATING', 'SENDING', 'SENT', 'DELETING', "
            "'DELETED', 'UNKNOWN')",
            name="ck_managed_gmail_drafts_status",
        ),
        sa.CheckConstraint(
            "char_length(current_content_hash) = 64", name="ck_managed_gmail_drafts_hash"
        ),
        sa.CheckConstraint(
            "send_proposal_version >= 0", name="ck_managed_gmail_drafts_send_version"
        ),
        sa.ForeignKeyConstraint(
            ["connector_account_id", "workspace_id", "user_id"],
            [
                "connector_accounts.id",
                "connector_accounts.workspace_id",
                "connector_accounts.user_id",
            ],
            name="fk_managed_gmail_drafts_connector_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["create_proposal_id", "workspace_id", "user_id"],
            ["action_proposals.id", "action_proposals.workspace_id", "action_proposals.user_id"],
            name="fk_managed_gmail_drafts_create_proposal_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["active_send_proposal_id", "workspace_id", "user_id"],
            ["action_proposals.id", "action_proposals.workspace_id", "action_proposals.user_id"],
            name="fk_managed_gmail_drafts_send_proposal_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", "user_id", name="uq_managed_gmail_drafts_id_scope"
        ),
        sa.UniqueConstraint(
            "connector_account_id",
            "provider_draft_id",
            name="uq_managed_gmail_drafts_provider_draft",
        ),
    )
    op.create_index(
        "ix_managed_gmail_drafts_scope_status",
        "managed_gmail_drafts",
        ["workspace_id", "user_id", "status"],
    )

    op.create_table(
        "action_revision_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        *_scope_columns(),
        sa.Column("telegram_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("managed_draft_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("active_send_proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'COMPLETED', 'EXPIRED', 'CANCELLED')",
            name="ck_action_revision_sessions_status",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_action_revision_sessions_expiry"),
        sa.ForeignKeyConstraint(
            ["telegram_account_id", "workspace_id", "user_id"],
            ["telegram_accounts.id", "telegram_accounts.workspace_id", "telegram_accounts.user_id"],
            name="fk_action_revision_sessions_account_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "workspace_id", "user_id"],
            [
                "telegram_conversations.id",
                "telegram_conversations.workspace_id",
                "telegram_conversations.user_id",
            ],
            name="fk_action_revision_sessions_conversation_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["managed_draft_id", "workspace_id", "user_id"],
            [
                "managed_gmail_drafts.id",
                "managed_gmail_drafts.workspace_id",
                "managed_gmail_drafts.user_id",
            ],
            name="fk_action_revision_sessions_draft_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["active_send_proposal_id", "workspace_id", "user_id"],
            ["action_proposals.id", "action_proposals.workspace_id", "action_proposals.user_id"],
            name="fk_action_revision_sessions_proposal_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", "user_id", name="uq_action_revision_sessions_id_scope"
        ),
    )
    op.create_index(
        "uq_action_revision_sessions_active_account",
        "action_revision_sessions",
        ["workspace_id", "user_id", "telegram_account_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_action_revision_sessions_active_account",
        table_name="action_revision_sessions",
    )
    op.drop_table("action_revision_sessions")
    op.drop_index("ix_managed_gmail_drafts_scope_status", table_name="managed_gmail_drafts")
    op.drop_table("managed_gmail_drafts")
    op.drop_table("action_results")
    op.drop_index("ix_actions_scope_status_lease", table_name="actions")
    op.drop_table("actions")
    op.drop_index(
        "ix_action_approvals_scope_status_expiry",
        table_name="action_approvals",
    )
    op.drop_table("action_approvals")
    op.drop_index("ix_action_proposals_scope_status", table_name="action_proposals")
    op.drop_table("action_proposals")
    op.drop_column("notifications", "reply_markup")
    op.drop_constraint("uq_conversation_turns_id_scope", "conversation_turns", type_="unique")
    op.drop_constraint("uq_connector_accounts_id_scope", "connector_accounts", type_="unique")
