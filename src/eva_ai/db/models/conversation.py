from datetime import datetime
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from eva_ai.conversation.types import (
    ConversationKind,
    ConversationStatus,
    ConversationTurnRole,
    ConversationTurnStatus,
)
from eva_ai.db.base import Base
from eva_ai.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin


class TelegramConversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "telegram_conversations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["telegram_account_id", "workspace_id", "user_id"],
            ["telegram_accounts.id", "telegram_accounts.workspace_id", "telegram_accounts.user_id"],
            name="fk_telegram_conversations_account_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_telegram_conversations_situation_scope",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "id", "workspace_id", "user_id", name="uq_telegram_conversations_id_scope"
        ),
        UniqueConstraint(
            "telegram_account_id",
            "situation_id",
            name="uq_telegram_conversations_account_situation",
        ),
        CheckConstraint("kind IN ('GENERAL', 'SITUATION')", name="ck_conversations_kind"),
        CheckConstraint("status IN ('ACTIVE', 'CLOSED')", name="ck_conversations_status"),
        CheckConstraint("next_sequence >= 1", name="ck_conversations_next_sequence"),
        Index(
            "ix_conversations_account_activity",
            "telegram_account_id",
            "status",
            "last_activity_at",
        ),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    telegram_account_id: Mapped[UUID]
    situation_id: Mapped[UUID]
    kind: Mapped[ConversationKind] = mapped_column(String(20))
    status: Mapped[ConversationStatus] = mapped_column(
        String(20), default=ConversationStatus.ACTIVE, server_default="ACTIVE"
    )
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    next_sequence: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConversationTurn(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "conversation_turns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "workspace_id", "user_id"],
            [
                "telegram_conversations.id",
                "telegram_conversations.workspace_id",
                "telegram_conversations.user_id",
            ],
            name="fk_conversation_turns_conversation_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_conversation_turns_event_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["notification_id", "workspace_id", "user_id"],
            ["notifications.id", "notifications.workspace_id", "notifications.user_id"],
            name="fk_conversation_turns_notification_scope",
            ondelete="CASCADE",
        ),
        UniqueConstraint("conversation_id", "sequence", name="uq_conversation_turns_sequence"),
        UniqueConstraint("event_id", name="uq_conversation_turns_event"),
        UniqueConstraint("notification_id", name="uq_conversation_turns_notification"),
        CheckConstraint("role IN ('USER', 'ASSISTANT')", name="ck_conversation_turns_role"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'RETRYABLE_FAILURE', "
            "'PERMANENT_FAILURE')",
            name="ck_conversation_turns_status",
        ),
        CheckConstraint("sequence >= 1", name="ck_conversation_turns_sequence"),
        CheckConstraint("attempt_count >= 0", name="ck_conversation_turns_attempt_count"),
        CheckConstraint("btrim(text) <> ''", name="ck_conversation_turns_text_nonblank"),
        Index(
            "ix_conversation_turns_scope_status_retry",
            "workspace_id",
            "user_id",
            "status",
            "next_retry_at",
        ),
        Index("ix_conversation_turns_conversation_sequence", "conversation_id", "sequence"),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    conversation_id: Mapped[UUID]
    role: Mapped[ConversationTurnRole] = mapped_column(String(20))
    status: Mapped[ConversationTurnStatus] = mapped_column(String(32))
    sequence: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    event_id: Mapped[UUID | None]
    notification_id: Mapped[UUID | None]
    agent_version: Mapped[str | None] = mapped_column(String(100))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_id: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_response_id: Mapped[str | None] = mapped_column(String(500))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tool_audit: Mapped[list[dict[str, JsonValue]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_summary: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
