"""Persist Gmail connector state.

Revision ID: 20260830_0003
Revises: 20260830_0002
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_0003"
down_revision: str | None = "20260830_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connector_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("account_identity", sa.String(length=320), nullable=False),
        sa.Column("granted_scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.String(length=40),
            server_default="CONNECTING",
            nullable=False,
        ),
        sa.Column("secret_reference", sa.String(length=500), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(length=200), nullable=True),
        sa.Column("last_error_summary", sa.String(length=500), nullable=True),
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
            "status IN ('CONNECTING', 'ACTIVE', 'REAUTHORIZATION_REQUIRED', 'DISABLED', 'ERROR')",
            name="ck_connector_accounts_status",
        ),
        sa.CheckConstraint(
            "status != 'ACTIVE' OR secret_reference IS NOT NULL",
            name="ck_connector_accounts_active_secret",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_connector_accounts_workspace_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "account_identity",
            name="uq_connector_accounts_workspace_provider_identity",
        ),
    )
    op.create_table(
        "gmail_sync_states",
        sa.Column("connector_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("history_id", sa.String(length=100), nullable=True),
        sa.Column("watch_expiration", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notification_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_successful_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_watch_renewal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_safety_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["connector_account_id"],
            ["connector_accounts.id"],
            name="fk_gmail_sync_states_connector_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("connector_account_id"),
    )
    op.create_index(
        "ix_gmail_sync_states_due",
        "gmail_sync_states",
        ["next_watch_renewal_at", "next_safety_sync_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_gmail_sync_states_due", table_name="gmail_sync_states")
    op.drop_table("gmail_sync_states")
    op.drop_table("connector_accounts")
