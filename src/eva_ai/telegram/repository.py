import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import UUID, uuid7

from sqlalchemy import or_, select

from eva_ai.db.models import TelegramAccount, TelegramPairingCode
from eva_ai.db.session import Database
from eva_ai.telegram.errors import (
    PairingCodeInvalidError,
    TelegramAccountConflictError,
    TelegramAccountNotFoundError,
)
from eva_ai.telegram.types import (
    PairingCodeRecord,
    PairingConsumeCommand,
    PairingLink,
    TelegramAccountRecord,
    TelegramAccountStatus,
)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TelegramAccountRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create_pairing(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        bot_username: str,
        now: datetime,
        ttl_seconds: int,
    ) -> PairingLink:
        token = secrets.token_urlsafe(32)
        row = TelegramPairingCode(
            id=uuid7(),
            user_id=user_id,
            workspace_id=workspace_id,
            code_digest=_digest(token),
            expires_at=now + timedelta(seconds=ttl_seconds),
            consumed_at=None,
            telegram_account_id=None,
            created_at=now,
        )
        async with self._database.session() as session:
            async with session.begin():
                session.add(row)
                await session.flush()
        username = bot_username.removeprefix("@").strip()
        return PairingLink(
            code_id=row.id,
            token=token,
            url=f"https://t.me/{username}?start={token}",
            expires_at=row.expires_at,
        )

    async def consume_pairing(self, command: PairingConsumeCommand) -> TelegramAccountRecord:
        async with self._database.session() as session:
            async with session.begin():
                code = await session.scalar(
                    select(TelegramPairingCode)
                    .where(TelegramPairingCode.code_digest == _digest(command.token))
                    .with_for_update()
                )
                if (
                    code is None
                    or code.consumed_at is not None
                    or code.expires_at <= command.consumed_at
                ):
                    raise PairingCodeInvalidError("pairing code is invalid")

                account = await session.scalar(
                    select(TelegramAccount)
                    .where(
                        or_(
                            TelegramAccount.telegram_user_id == command.telegram_user_id,
                            TelegramAccount.chat_id == command.chat_id,
                        )
                    )
                    .with_for_update()
                )
                if account is None:
                    account = await session.scalar(
                        select(TelegramAccount)
                        .where(
                            TelegramAccount.workspace_id == code.workspace_id,
                            TelegramAccount.user_id == code.user_id,
                        )
                        .with_for_update()
                    )
                if account is not None:
                    if (
                        account.user_id != code.user_id
                        or account.workspace_id != code.workspace_id
                        or account.telegram_user_id != command.telegram_user_id
                        or account.chat_id != command.chat_id
                    ):
                        raise TelegramAccountConflictError(
                            "Telegram identity is paired to another account"
                        )
                    account.status = TelegramAccountStatus.ACTIVE
                    account.username = command.username
                    account.first_name = command.first_name
                    account.paired_at = command.consumed_at
                    account.revoked_at = None
                else:
                    account = TelegramAccount(
                        id=uuid7(),
                        user_id=code.user_id,
                        workspace_id=code.workspace_id,
                        telegram_user_id=command.telegram_user_id,
                        chat_id=command.chat_id,
                        username=command.username,
                        first_name=command.first_name,
                        status=TelegramAccountStatus.ACTIVE,
                        paired_at=command.consumed_at,
                        revoked_at=None,
                    )
                    session.add(account)
                    await session.flush()
                code.consumed_at = command.consumed_at
                code.telegram_account_id = account.id
                await session.flush()
                return _account_record(account)

    async def find_active(
        self, *, telegram_user_id: int, chat_id: int
    ) -> TelegramAccountRecord | None:
        statement = select(TelegramAccount).where(
            TelegramAccount.telegram_user_id == telegram_user_id,
            TelegramAccount.chat_id == chat_id,
            TelegramAccount.status == TelegramAccountStatus.ACTIVE,
        )
        async with self._database.session() as session:
            row = await session.scalar(statement)
        return None if row is None else _account_record(row)

    async def get_active_for_scope(
        self, *, user_id: UUID, workspace_id: UUID
    ) -> TelegramAccountRecord | None:
        statement = select(TelegramAccount).where(
            TelegramAccount.user_id == user_id,
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.status == TelegramAccountStatus.ACTIVE,
        )
        async with self._database.session() as session:
            row = await session.scalar(statement)
        return None if row is None else _account_record(row)

    async def list(self, *, user_id: UUID, workspace_id: UUID) -> tuple[TelegramAccountRecord, ...]:
        statement = (
            select(TelegramAccount)
            .where(
                TelegramAccount.user_id == user_id,
                TelegramAccount.workspace_id == workspace_id,
            )
            .order_by(TelegramAccount.created_at.desc())
        )
        async with self._database.session() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(_account_record(row) for row in rows)

    async def revoke(
        self, *, account_id: UUID, user_id: UUID, workspace_id: UUID, now: datetime
    ) -> TelegramAccountRecord:
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(TelegramAccount)
                    .where(
                        TelegramAccount.id == account_id,
                        TelegramAccount.user_id == user_id,
                        TelegramAccount.workspace_id == workspace_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise TelegramAccountNotFoundError("Telegram account not found")
                row.status = TelegramAccountStatus.REVOKED
                row.revoked_at = now
                await session.flush()
                return _account_record(row)

    async def pairing_codes(
        self, *, user_id: UUID, workspace_id: UUID
    ) -> tuple[PairingCodeRecord, ...]:
        statement = (
            select(TelegramPairingCode)
            .where(
                TelegramPairingCode.user_id == user_id,
                TelegramPairingCode.workspace_id == workspace_id,
            )
            .order_by(TelegramPairingCode.created_at.desc())
        )
        async with self._database.session() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(_pairing_record(row) for row in rows)


def _account_record(row: TelegramAccount) -> TelegramAccountRecord:
    return TelegramAccountRecord(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        telegram_user_id=row.telegram_user_id,
        chat_id=row.chat_id,
        username=row.username,
        first_name=row.first_name,
        status=TelegramAccountStatus(row.status),
        paired_at=row.paired_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _pairing_record(row: TelegramPairingCode) -> PairingCodeRecord:
    return PairingCodeRecord(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
        telegram_account_id=row.telegram_account_id,
        created_at=row.created_at,
    )
