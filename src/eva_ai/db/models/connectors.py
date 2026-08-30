from datetime import datetime
from uuid import UUID

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
from sqlalchemy.orm import Mapped, mapped_column

from eva_ai.connectors.types import ConnectorStatus
from eva_ai.db.base import Base
from eva_ai.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin

CONNECTOR_ACCOUNT_SCOPE_FK = ForeignKeyConstraint(
    ["workspace_id", "user_id"],
    ["workspaces.id", "workspaces.user_id"],
    name="fk_connector_accounts_workspace_user",
    ondelete="CASCADE",
)


class ConnectorAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "connector_accounts"
    __table_args__ = (
        CONNECTOR_ACCOUNT_SCOPE_FK,
        UniqueConstraint(
            "workspace_id",
            "provider",
            "account_identity",
            name="uq_connector_accounts_workspace_provider_identity",
        ),
        CheckConstraint(
            "status IN ('CONNECTING', 'ACTIVE', 'REAUTHORIZATION_REQUIRED', 'DISABLED', 'ERROR')",
            name="ck_connector_accounts_status",
        ),
        CheckConstraint(
            "status != 'ACTIVE' OR secret_reference IS NOT NULL",
            name="ck_connector_accounts_active_secret",
        ),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    provider: Mapped[str] = mapped_column(String(100))
    account_identity: Mapped[str] = mapped_column(String(320))
    granted_scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    status: Mapped[ConnectorStatus] = mapped_column(String(40), server_default="CONNECTING")
    secret_reference: Mapped[str | None] = mapped_column(String(500))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String(200))
    last_error_summary: Mapped[str | None] = mapped_column(String(500))


class GmailSyncState(TimestampMixin, Base):
    __tablename__ = "gmail_sync_states"
    __table_args__ = (
        Index(
            "ix_gmail_sync_states_due",
            "next_watch_renewal_at",
            "next_safety_sync_at",
        ),
    )

    connector_account_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "connector_accounts.id",
            name="fk_gmail_sync_states_connector_account",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    history_id: Mapped[str | None] = mapped_column(String(100))
    watch_expiration: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_notification_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_watch_renewal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_safety_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_id: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
