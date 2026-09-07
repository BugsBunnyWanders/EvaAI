from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from eva_ai.db.base import Base
from eva_ai.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin
from eva_ai.telegram.types import TelegramAccountStatus


class TelegramAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "telegram_accounts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_telegram_accounts_workspace_user",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "workspace_id", "user_id", name="uq_telegram_accounts_id_scope"),
        UniqueConstraint("telegram_user_id", name="uq_telegram_accounts_user"),
        UniqueConstraint("chat_id", name="uq_telegram_accounts_chat"),
        UniqueConstraint("workspace_id", "user_id", name="uq_telegram_accounts_workspace_user"),
        CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="ck_telegram_accounts_status"),
        CheckConstraint("telegram_user_id > 0", name="ck_telegram_accounts_user_positive"),
        Index("ix_telegram_accounts_scope_status", "workspace_id", "user_id", "status"),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(100))
    first_name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[TelegramAccountStatus] = mapped_column(
        String(20), default=TelegramAccountStatus.ACTIVE, server_default="ACTIVE"
    )
    paired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramPairingCode(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "telegram_pairing_codes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_telegram_pairing_codes_workspace_user",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["telegram_account_id", "workspace_id", "user_id"],
            ["telegram_accounts.id", "telegram_accounts.workspace_id", "telegram_accounts.user_id"],
            name="fk_telegram_pairing_codes_account_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("code_digest", name="uq_telegram_pairing_codes_digest"),
        CheckConstraint("char_length(code_digest) = 64", name="ck_pairing_code_digest_length"),
        Index("ix_pairing_codes_scope_expiry", "workspace_id", "user_id", "expires_at"),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    code_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_account_id: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
