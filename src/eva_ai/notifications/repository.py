from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid5, uuid7

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from eva_ai.agent.types import NotificationUrgency
from eva_ai.db.models import Notification, OutboxMessage, TelegramAccount
from eva_ai.db.session import Database
from eva_ai.events.types import OutboxState
from eva_ai.notifications.errors import (
    NotificationConflictError,
    NotificationNotFoundError,
    NotificationScopeError,
)
from eva_ai.notifications.types import (
    NotificationChannel,
    NotificationDeliveryRequestedMessage,
    NotificationKind,
    NotificationRecord,
    NotificationStatus,
)
from eva_ai.telegram.types import TelegramAccountStatus

_NOTIFICATION_NAMESPACE = UUID("8e76fa34-5b80-53e6-83cd-b6678991d113")
_OUTBOX_NAMESPACE = UUID("c73d6c50-2c75-58b8-a92b-45df53fa61b0")


@dataclass(frozen=True, slots=True)
class NotificationClaim:
    notification_id: UUID
    claim_id: UUID
    user_id: UUID
    workspace_id: UUID
    attempt_count: int


@dataclass(frozen=True, slots=True)
class NotificationDeliverySubject:
    notification: NotificationRecord
    telegram_account_id: UUID
    chat_id: int


class NotificationRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create_in_session(
        self,
        session: AsyncSession,
        *,
        event_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID | None,
        agent_run_id: UUID | None,
        kind: NotificationKind,
        urgency: NotificationUrgency,
        message: str,
        dedupe_key: str,
        destination: str,
        created_at: datetime,
    ) -> NotificationRecord:
        notification_id = uuid5(_NOTIFICATION_NAMESPACE, f"{workspace_id}:{dedupe_key}")
        statement = (
            insert(Notification)
            .values(
                id=notification_id,
                event_id=event_id,
                user_id=user_id,
                workspace_id=workspace_id,
                situation_id=situation_id,
                agent_run_id=agent_run_id,
                channel=NotificationChannel.TELEGRAM,
                kind=kind,
                urgency=urgency,
                message=message,
                dedupe_key=dedupe_key,
                status=NotificationStatus.PENDING,
                created_at=created_at,
            )
            .on_conflict_do_nothing(constraint="uq_notifications_scope_dedupe")
            .returning(Notification)
        )
        row = (await session.scalars(statement)).one_or_none()
        if row is None:
            row = await session.scalar(
                select(Notification).where(
                    Notification.workspace_id == workspace_id,
                    Notification.user_id == user_id,
                    Notification.dedupe_key == dedupe_key,
                )
            )
            if row is None:
                raise NotificationConflictError("Notification was not visible after dedupe")
            return _record(row)

        outbox_id = uuid5(_OUTBOX_NAMESPACE, str(notification_id))
        envelope = NotificationDeliveryRequestedMessage(
            outbox_message_id=outbox_id,
            notification_id=notification_id,
            event_id=event_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        session.add(
            OutboxMessage(
                id=outbox_id,
                event_id=event_id,
                destination=destination,
                message_type=envelope.message_type,
                schema_version=envelope.schema_version,
                payload=envelope.model_dump(mode="json"),
                state=OutboxState.PENDING,
                available_at=created_at,
            )
        )
        await session.flush()
        return _record(row)

    async def claim(
        self,
        message: NotificationDeliveryRequestedMessage,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> NotificationClaim | None:
        claim_id = uuid7()
        eligible = or_(
            Notification.status == NotificationStatus.PENDING,
            and_(
                Notification.status == NotificationStatus.RETRYABLE_FAILURE,
                or_(Notification.next_retry_at.is_(None), Notification.next_retry_at <= now),
            ),
            and_(
                Notification.status == NotificationStatus.SENDING,
                Notification.lease_expires_at <= now,
            ),
        )
        statement = (
            update(Notification)
            .where(
                Notification.id == message.notification_id,
                Notification.event_id == message.event_id,
                Notification.user_id == message.user_id,
                Notification.workspace_id == message.workspace_id,
                eligible,
            )
            .values(
                status=NotificationStatus.SENDING,
                claim_id=claim_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempt_count=Notification.attempt_count + 1,
                next_retry_at=None,
                failure_code=None,
                failure_summary=None,
            )
            .returning(Notification.attempt_count)
        )
        async with self._database.session() as session:
            async with session.begin():
                attempt = (await session.execute(statement)).scalar_one_or_none()
        if attempt is None:
            return None
        return NotificationClaim(
            notification_id=message.notification_id,
            claim_id=claim_id,
            user_id=message.user_id,
            workspace_id=message.workspace_id,
            attempt_count=attempt,
        )

    async def load_delivery_subject(self, claim: NotificationClaim) -> NotificationDeliverySubject:
        statement = (
            select(Notification, TelegramAccount)
            .join(
                TelegramAccount,
                and_(
                    TelegramAccount.user_id == Notification.user_id,
                    TelegramAccount.workspace_id == Notification.workspace_id,
                    TelegramAccount.status == TelegramAccountStatus.ACTIVE,
                ),
            )
            .where(_claim_predicate(claim))
        )
        async with self._database.session() as session:
            values = (await session.execute(statement)).one_or_none()
        if values is None:
            raise NotificationScopeError("active Telegram delivery target is unavailable")
        notification, account = values
        return NotificationDeliverySubject(
            notification=_record(notification),
            telegram_account_id=account.id,
            chat_id=account.chat_id,
        )

    async def mark_sent(
        self,
        claim: NotificationClaim,
        *,
        telegram_account_id: UUID,
        chat_id: int,
        provider_message_id: int,
        sent_at: datetime,
    ) -> NotificationRecord:
        statement = (
            update(Notification)
            .where(_claim_predicate(claim))
            .values(
                status=NotificationStatus.SENT,
                telegram_account_id=telegram_account_id,
                provider_chat_id=chat_id,
                provider_message_id=provider_message_id,
                sent_at=sent_at,
                claim_id=None,
                lease_expires_at=None,
                next_retry_at=None,
                failure_code=None,
                failure_summary=None,
            )
            .returning(Notification)
        )
        return await self._finish(statement)

    async def fail(
        self,
        claim: NotificationClaim,
        *,
        retryable: bool,
        max_attempts: int,
        next_retry_at: datetime | None,
        failure_code: str,
        failure_summary: str,
    ) -> NotificationRecord:
        should_retry = retryable and claim.attempt_count < max_attempts
        statement = (
            update(Notification)
            .where(_claim_predicate(claim))
            .values(
                status=(
                    NotificationStatus.RETRYABLE_FAILURE
                    if should_retry
                    else NotificationStatus.PERMANENT_FAILURE
                ),
                next_retry_at=next_retry_at if should_retry else None,
                claim_id=None,
                lease_expires_at=None,
                failure_code=failure_code,
                failure_summary=failure_summary,
            )
            .returning(Notification)
        )
        return await self._finish(statement)

    async def get(
        self, *, notification_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> NotificationRecord | None:
        statement = select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
            Notification.workspace_id == workspace_id,
        )
        async with self._database.session() as session:
            row = await session.scalar(statement)
        return None if row is None else _record(row)

    async def list(
        self, *, user_id: UUID, workspace_id: UUID, limit: int = 50
    ) -> tuple[NotificationRecord, ...]:
        statement = (
            select(Notification)
            .where(Notification.user_id == user_id, Notification.workspace_id == workspace_id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
        )
        async with self._database.session() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(_record(row) for row in rows)

    async def retry(
        self,
        *,
        notification_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        destination: str,
        requested_at: datetime,
    ) -> NotificationRecord:
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(Notification)
                    .where(
                        Notification.id == notification_id,
                        Notification.user_id == user_id,
                        Notification.workspace_id == workspace_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise NotificationNotFoundError("Notification not found")
                if row.status not in {
                    NotificationStatus.RETRYABLE_FAILURE,
                    NotificationStatus.PERMANENT_FAILURE,
                }:
                    raise NotificationConflictError(
                        "Notification cannot be retried from its current state"
                    )
                outbox_id = uuid7()
                envelope = NotificationDeliveryRequestedMessage(
                    outbox_message_id=outbox_id,
                    notification_id=row.id,
                    event_id=row.event_id,
                    user_id=row.user_id,
                    workspace_id=row.workspace_id,
                )
                row.status = NotificationStatus.PENDING
                row.next_retry_at = None
                row.failure_code = None
                row.failure_summary = None
                session.add(
                    OutboxMessage(
                        id=outbox_id,
                        event_id=row.event_id,
                        destination=destination,
                        message_type=envelope.message_type,
                        schema_version=envelope.schema_version,
                        payload=envelope.model_dump(mode="json"),
                        state=OutboxState.PENDING,
                        available_at=requested_at,
                    )
                )
                await session.flush()
                return _record(row)

    async def _finish(self, statement: Any) -> NotificationRecord:
        async with self._database.session() as session:
            async with session.begin():
                row = (await session.scalars(statement)).one_or_none()
        if row is None:
            raise NotificationConflictError("Notification claim is stale")
        return _record(row)


def _claim_predicate(claim: NotificationClaim) -> Any:
    return and_(
        Notification.id == claim.notification_id,
        Notification.user_id == claim.user_id,
        Notification.workspace_id == claim.workspace_id,
        Notification.status == NotificationStatus.SENDING,
        Notification.claim_id == claim.claim_id,
    )


def _record(row: Notification) -> NotificationRecord:
    return NotificationRecord(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        event_id=row.event_id,
        situation_id=row.situation_id,
        agent_run_id=row.agent_run_id,
        channel=NotificationChannel(row.channel),
        kind=NotificationKind(row.kind),
        urgency=NotificationUrgency(row.urgency),
        message=row.message,
        dedupe_key=row.dedupe_key,
        status=NotificationStatus(row.status),
        attempt_count=row.attempt_count,
        next_retry_at=row.next_retry_at,
        claim_id=row.claim_id,
        lease_expires_at=row.lease_expires_at,
        telegram_account_id=row.telegram_account_id,
        provider_chat_id=row.provider_chat_id,
        provider_message_id=row.provider_message_id,
        failure_code=row.failure_code,
        failure_summary=row.failure_summary,
        created_at=row.created_at,
        sent_at=row.sent_at,
    )
