from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from eva_ai.agent.types import NotificationUrgency
from eva_ai.db.base import Base
from eva_ai.db.models.common import UUIDPrimaryKeyMixin
from eva_ai.notifications.types import NotificationChannel, NotificationKind, NotificationStatus


class Notification(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_notifications_workspace_user",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_notifications_event_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_notifications_situation_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["agent_run_id", "workspace_id", "user_id"],
            ["agent_runs.id", "agent_runs.workspace_id", "agent_runs.user_id"],
            name="fk_notifications_agent_run_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["telegram_account_id", "workspace_id", "user_id"],
            ["telegram_accounts.id", "telegram_accounts.workspace_id", "telegram_accounts.user_id"],
            name="fk_notifications_telegram_account_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "workspace_id", "user_id", name="uq_notifications_id_scope"),
        UniqueConstraint("workspace_id", "dedupe_key", name="uq_notifications_scope_dedupe"),
        UniqueConstraint(
            "telegram_account_id",
            "provider_message_id",
            name="uq_notifications_provider_message",
        ),
        CheckConstraint("channel IN ('TELEGRAM')", name="ck_notifications_channel"),
        CheckConstraint("kind IN ('PROACTIVE', 'REACTIVE')", name="ck_notifications_kind"),
        CheckConstraint("urgency IN ('low', 'medium', 'high')", name="ck_notifications_urgency"),
        CheckConstraint(
            "status IN ('PENDING', 'SENDING', 'SENT', 'RETRYABLE_FAILURE', 'PERMANENT_FAILURE')",
            name="ck_notifications_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_notifications_attempt_count"),
        CheckConstraint("btrim(message) <> ''", name="ck_notifications_message_nonblank"),
        Index(
            "ix_notifications_scope_status_retry",
            "workspace_id",
            "user_id",
            "status",
            "next_retry_at",
        ),
        Index("ix_notifications_situation_created", "situation_id", "created_at"),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    event_id: Mapped[UUID]
    situation_id: Mapped[UUID | None]
    agent_run_id: Mapped[UUID | None]
    channel: Mapped[NotificationChannel] = mapped_column(String(20))
    kind: Mapped[NotificationKind] = mapped_column(String(20))
    urgency: Mapped[NotificationUrgency] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(500))
    status: Mapped[NotificationStatus] = mapped_column(
        String(32), default=NotificationStatus.PENDING, server_default="PENDING"
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_id: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_account_id: Mapped[UUID | None]
    provider_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    provider_message_id: Mapped[int | None] = mapped_column(BigInteger)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_summary: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
