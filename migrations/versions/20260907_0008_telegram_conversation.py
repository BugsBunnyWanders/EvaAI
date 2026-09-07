"""Add authenticated Telegram conversations and notifications.

Revision ID: 20260907_0008
Revises: 20260907_0007
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260907_0008"
down_revision: str | None = "20260907_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_situations_type", "situations", type_="check")
    op.create_check_constraint(
        "ck_situations_type", "situations", "type IN ('EMAIL_THREAD', 'TELEGRAM_CHAT')"
    )
    op.drop_constraint(
        "ck_situation_correlation_keys_kind", "situation_correlation_keys", type_="check"
    )
    op.create_check_constraint(
        "ck_situation_correlation_keys_kind",
        "situation_correlation_keys",
        "kind IN ('GMAIL_THREAD', 'TELEGRAM_CONVERSATION')",
    )

    op.create_table(
        "telegram_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=True),
        sa.Column("first_name", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="ACTIVE", nullable=False),
        sa.Column("paired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="ck_telegram_accounts_status"),
        sa.CheckConstraint("telegram_user_id > 0", name="ck_telegram_accounts_user_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_telegram_accounts_workspace_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", "user_id", name="uq_telegram_accounts_id_scope"),
        sa.UniqueConstraint("telegram_user_id", name="uq_telegram_accounts_user"),
        sa.UniqueConstraint("chat_id", name="uq_telegram_accounts_chat"),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_telegram_accounts_workspace_user"),
    )
    op.create_index(
        "ix_telegram_accounts_scope_status",
        "telegram_accounts",
        ["workspace_id", "user_id", "status"],
    )

    op.create_table(
        "telegram_pairing_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("char_length(code_digest) = 64", name="ck_pairing_code_digest_length"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_telegram_pairing_codes_workspace_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["telegram_account_id", "workspace_id", "user_id"],
            ["telegram_accounts.id", "telegram_accounts.workspace_id", "telegram_accounts.user_id"],
            name="fk_telegram_pairing_codes_account_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_digest", name="uq_telegram_pairing_codes_digest"),
    )
    op.create_index(
        "ix_pairing_codes_scope_expiry",
        "telegram_pairing_codes",
        ["workspace_id", "user_id", "expires_at"],
    )

    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("situation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("urgency", sa.String(length=20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("provider_message_id", sa.BigInteger(), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("channel IN ('TELEGRAM')", name="ck_notifications_channel"),
        sa.CheckConstraint("kind IN ('PROACTIVE', 'REACTIVE')", name="ck_notifications_kind"),
        sa.CheckConstraint("urgency IN ('low', 'medium', 'high')", name="ck_notifications_urgency"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SENDING', 'SENT', 'RETRYABLE_FAILURE', 'PERMANENT_FAILURE')",
            name="ck_notifications_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_notifications_attempt_count"),
        sa.CheckConstraint("btrim(message) <> ''", name="ck_notifications_message_nonblank"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_notifications_workspace_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_notifications_event_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_notifications_situation_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id", "workspace_id", "user_id"],
            ["agent_runs.id", "agent_runs.workspace_id", "agent_runs.user_id"],
            name="fk_notifications_agent_run_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["telegram_account_id", "workspace_id", "user_id"],
            ["telegram_accounts.id", "telegram_accounts.workspace_id", "telegram_accounts.user_id"],
            name="fk_notifications_telegram_account_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", "user_id", name="uq_notifications_id_scope"),
        sa.UniqueConstraint("workspace_id", "dedupe_key", name="uq_notifications_scope_dedupe"),
        sa.UniqueConstraint(
            "telegram_account_id",
            "provider_message_id",
            name="uq_notifications_provider_message",
        ),
    )
    op.create_index(
        "ix_notifications_scope_status_retry",
        "notifications",
        ["workspace_id", "user_id", "status", "next_retry_at"],
    )
    op.create_index(
        "ix_notifications_situation_created",
        "notifications",
        ["situation_id", "created_at"],
    )

    op.create_table(
        "telegram_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("telegram_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("situation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="ACTIVE", nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("next_sequence", sa.Integer(), server_default="1", nullable=False),
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
        sa.CheckConstraint("kind IN ('GENERAL', 'SITUATION')", name="ck_conversations_kind"),
        sa.CheckConstraint("status IN ('ACTIVE', 'CLOSED')", name="ck_conversations_status"),
        sa.CheckConstraint("next_sequence >= 1", name="ck_conversations_next_sequence"),
        sa.ForeignKeyConstraint(
            ["telegram_account_id", "workspace_id", "user_id"],
            ["telegram_accounts.id", "telegram_accounts.workspace_id", "telegram_accounts.user_id"],
            name="fk_telegram_conversations_account_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_telegram_conversations_situation_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "workspace_id", "user_id", name="uq_telegram_conversations_id_scope"
        ),
        sa.UniqueConstraint(
            "telegram_account_id",
            "situation_id",
            name="uq_telegram_conversations_account_situation",
        ),
    )
    op.create_index(
        "ix_conversations_account_activity",
        "telegram_conversations",
        ["telegram_account_id", "status", "last_activity_at"],
    )

    op.create_table(
        "conversation_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_version", sa.String(length=100), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
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
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('USER', 'ASSISTANT')", name="ck_conversation_turns_role"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'RETRYABLE_FAILURE', "
            "'PERMANENT_FAILURE')",
            name="ck_conversation_turns_status",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_conversation_turns_sequence"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_conversation_turns_attempt_count"),
        sa.CheckConstraint("btrim(text) <> ''", name="ck_conversation_turns_text_nonblank"),
        sa.ForeignKeyConstraint(
            ["conversation_id", "workspace_id", "user_id"],
            [
                "telegram_conversations.id",
                "telegram_conversations.workspace_id",
                "telegram_conversations.user_id",
            ],
            name="fk_conversation_turns_conversation_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_conversation_turns_event_scope",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["notification_id", "workspace_id", "user_id"],
            ["notifications.id", "notifications.workspace_id", "notifications.user_id"],
            name="fk_conversation_turns_notification_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_conversation_turns_sequence"),
        sa.UniqueConstraint("event_id", name="uq_conversation_turns_event"),
        sa.UniqueConstraint("notification_id", name="uq_conversation_turns_notification"),
    )
    op.create_index(
        "ix_conversation_turns_scope_status_retry",
        "conversation_turns",
        ["workspace_id", "user_id", "status", "next_retry_at"],
    )
    op.create_index(
        "ix_conversation_turns_conversation_sequence",
        "conversation_turns",
        ["conversation_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_turns_conversation_sequence", table_name="conversation_turns")
    op.drop_index("ix_conversation_turns_scope_status_retry", table_name="conversation_turns")
    op.drop_table("conversation_turns")
    op.drop_index("ix_conversations_account_activity", table_name="telegram_conversations")
    op.drop_table("telegram_conversations")
    op.drop_index("ix_notifications_situation_created", table_name="notifications")
    op.drop_index("ix_notifications_scope_status_retry", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_pairing_codes_scope_expiry", table_name="telegram_pairing_codes")
    op.drop_table("telegram_pairing_codes")
    op.drop_index("ix_telegram_accounts_scope_status", table_name="telegram_accounts")
    op.drop_table("telegram_accounts")

    # These values cannot exist under the Milestone 6 checks. Remove only records introduced by
    # this revision before narrowing the constraints, so downgrade works after real chat traffic.
    op.execute(
        sa.text("DELETE FROM situation_correlation_keys WHERE kind = 'TELEGRAM_CONVERSATION'")
    )
    op.execute(sa.text("DELETE FROM situations WHERE type = 'TELEGRAM_CHAT'"))

    op.drop_constraint(
        "ck_situation_correlation_keys_kind", "situation_correlation_keys", type_="check"
    )
    op.create_check_constraint(
        "ck_situation_correlation_keys_kind",
        "situation_correlation_keys",
        "kind IN ('GMAIL_THREAD')",
    )
    op.drop_constraint("ck_situations_type", "situations", type_="check")
    op.create_check_constraint("ck_situations_type", "situations", "type IN ('EMAIL_THREAD')")
