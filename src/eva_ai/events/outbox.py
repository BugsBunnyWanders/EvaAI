import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid7

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult

from eva_ai.db.models import OutboxMessage
from eva_ai.db.session import Database
from eva_ai.events.errors import StaleClaimError, sanitize_error
from eva_ai.events.publisher import Publisher
from eva_ai.events.types import EventAvailableMessage, OutboundMessage, OutboxState

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClaimedOutboxMessage:
    id: UUID
    claim_id: UUID
    event_id: UUID
    destination: str
    envelope: EventAvailableMessage
    attempt_count: int

    def outbound(self) -> OutboundMessage:
        return OutboundMessage(self.id, self.destination, self.envelope)


@dataclass(frozen=True, slots=True)
class PublishBatchResult:
    claimed: int
    published: int
    failed: int


class OutboxRelay:
    def __init__(self, database: Database, publisher: Publisher, lease_seconds: int) -> None:
        self._database = database
        self._publisher = publisher
        self._lease_seconds = lease_seconds

    async def claim_batch(
        self,
        limit: int,
        now: datetime | None = None,
    ) -> list[ClaimedOutboxMessage]:
        effective_now = now or datetime.now(UTC)
        eligible = or_(
            and_(
                OutboxMessage.state == OutboxState.PENDING,
                OutboxMessage.available_at <= effective_now,
            ),
            and_(
                OutboxMessage.state == OutboxState.PUBLISHING,
                OutboxMessage.lease_expires_at <= effective_now,
            ),
        )
        statement = (
            select(OutboxMessage)
            .where(eligible)
            .order_by(OutboxMessage.available_at, OutboxMessage.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        claimed: list[ClaimedOutboxMessage] = []
        async with self._database.session() as session:
            async with session.begin():
                rows = (await session.scalars(statement)).all()
                for row in rows:
                    claim_id = uuid7()
                    row.state = OutboxState.PUBLISHING
                    row.claim_id = claim_id
                    row.lease_expires_at = effective_now + timedelta(seconds=self._lease_seconds)
                    row.attempt_count += 1
                    row.last_error_type = None
                    row.last_error_summary = None
                    claimed.append(
                        ClaimedOutboxMessage(
                            id=row.id,
                            claim_id=claim_id,
                            event_id=row.event_id,
                            destination=row.destination,
                            envelope=EventAvailableMessage.model_validate(row.payload),
                            attempt_count=row.attempt_count,
                        )
                    )

        # Network publication deliberately happens after row locks and the transaction are gone.
        return claimed

    async def complete_claim(
        self,
        message_id: UUID,
        claim_id: UUID,
        provider_message_id: str,
        published_at: datetime | None = None,
    ) -> None:
        statement = (
            update(OutboxMessage)
            .where(
                OutboxMessage.id == message_id,
                OutboxMessage.state == OutboxState.PUBLISHING,
                OutboxMessage.claim_id == claim_id,
            )
            .values(
                state=OutboxState.PUBLISHED,
                provider_message_id=provider_message_id,
                published_at=published_at or datetime.now(UTC),
                claim_id=None,
                lease_expires_at=None,
                last_error_type=None,
                last_error_summary=None,
            )
        )
        async with self._database.session() as session:
            async with session.begin():
                result = cast(CursorResult[Any], await session.execute(statement))
                if result.rowcount != 1:
                    raise StaleClaimError("outbox claim is no longer current")

    async def release_claim(
        self,
        message_id: UUID,
        claim_id: UUID,
        error: BaseException,
    ) -> None:
        stored_error = sanitize_error(error)
        statement = (
            update(OutboxMessage)
            .where(
                OutboxMessage.id == message_id,
                OutboxMessage.state == OutboxState.PUBLISHING,
                OutboxMessage.claim_id == claim_id,
            )
            .values(
                state=OutboxState.PENDING,
                claim_id=None,
                lease_expires_at=None,
                last_error_type=stored_error.error_type,
                last_error_summary=stored_error.summary,
            )
        )
        async with self._database.session() as session:
            async with session.begin():
                result = cast(CursorResult[Any], await session.execute(statement))
                if result.rowcount != 1:
                    raise StaleClaimError("outbox claim is no longer current")

    async def publish_batch(self, limit: int) -> PublishBatchResult:
        claimed = await self.claim_batch(limit)
        published = 0
        failed = 0
        for item in claimed:
            log_context = {
                "event_id": str(item.event_id),
                "outbox_message_id": str(item.id),
                "claim_id": str(item.claim_id),
            }
            try:
                provider_id = await self._publisher.publish(item.outbound())
                await self.complete_claim(item.id, item.claim_id, provider_id)
                published += 1
                _LOGGER.info(
                    "outbox publication finished",
                    extra={**log_context, "outcome": "published"},
                )
            except Exception as error:
                try:
                    await self.release_claim(item.id, item.claim_id, error)
                except StaleClaimError:
                    failed += 1
                    _LOGGER.info(
                        "outbox publication finished",
                        extra={**log_context, "outcome": "stale"},
                    )
                    continue
                failed += 1
                _LOGGER.info(
                    "outbox publication finished",
                    extra={**log_context, "outcome": "failed"},
                )

        return PublishBatchResult(len(claimed), published, failed)
