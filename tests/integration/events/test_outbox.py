import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete, select

from eva_ai.db import Database
from eva_ai.db.models import OutboxMessage
from eva_ai.events import (
    ClaimedOutboxMessage,
    EventService,
    OutboxRelay,
    PublishBatchResult,
    StaleClaimError,
)
from eva_ai.events.publisher import InMemoryPublisher
from eva_ai.events.types import NewEvent, OutboundMessage, OutboxState, PrincipalType
from tests.integration.factories import create_scope

FIXED_NOW = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def isolate_outbox(database: Database) -> AsyncIterator[None]:
    async with database.session() as session:
        async with session.begin():
            await session.execute(delete(OutboxMessage))
    yield
    async with database.session() as session:
        async with session.begin():
            await session.execute(delete(OutboxMessage))


async def create_pending_messages(
    database: Database,
    *,
    count: int,
    received_at: datetime,
) -> list[UUID]:
    scope = await create_scope(database)
    message_ids: list[UUID] = []
    for index in range(count):
        command = NewEvent(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            source="test",
            event_type="test.created",
            idempotency_key=f"outbox-{scope.workspace_id}-{index}",
            occurred_at=received_at,
            received_at=received_at,
            principal_type=PrincipalType.SYSTEM,
        )
        result = await EventService(database, "eva-events").ingest(command)
        async with database.session() as session:
            message_id = await session.scalar(
                select(OutboxMessage.id).where(OutboxMessage.event_id == result.event_id)
            )
        assert message_id is not None
        message_ids.append(message_id)

    return message_ids


async def load_outbox(database: Database, message_id: UUID) -> OutboxMessage:
    async with database.session() as session:
        row = await session.get(OutboxMessage, message_id)
    assert row is not None
    return row


class FailingPublisher:
    async def publish(self, message: OutboundMessage) -> str:
        raise RuntimeError(f"provider-token for {message.outbox_message_id}")


class SelectiveFailingPublisher:
    def __init__(self, failing_message_id: UUID) -> None:
        self._failing_message_id = failing_message_id

    async def publish(self, message: OutboundMessage) -> str:
        if message.outbox_message_id == self._failing_message_id:
            raise RuntimeError("provider-token")
        return f"selective:{message.outbox_message_id}"


class ReclaimingFailFirstPublisher:
    def __init__(self, reclaiming_relay: OutboxRelay, reclaim_at: datetime) -> None:
        self._reclaiming_relay = reclaiming_relay
        self._reclaim_at = reclaim_at
        self.reclaimed: ClaimedOutboxMessage | None = None

    async def publish(self, message: OutboundMessage) -> str:
        if self.reclaimed is None:
            self.reclaimed = (await self._reclaiming_relay.claim_batch(1, self._reclaim_at))[0]
            raise RuntimeError(f"provider-token for {message.outbox_message_id}")
        return f"after-stale:{message.outbox_message_id}"


@pytest.mark.integration
async def test_two_relays_claim_disjoint_rows(database: Database) -> None:
    message_ids = await create_pending_messages(
        database,
        count=4,
        received_at=datetime.now(UTC),
    )
    relay_one = OutboxRelay(database, InMemoryPublisher(), lease_seconds=60)
    relay_two = OutboxRelay(database, InMemoryPublisher(), lease_seconds=60)

    first, second = await asyncio.gather(relay_one.claim_batch(2), relay_two.claim_batch(2))

    assert len(first) == len(second) == 2
    assert {item.id for item in first}.isdisjoint(item.id for item in second)
    assert {item.id for item in first + second} == set(message_ids)
    assert all(item.claim_id is not None for item in first + second)


@pytest.mark.integration
async def test_expired_outbox_lease_is_reclaimable(database: Database) -> None:
    message_id = (await create_pending_messages(database, count=1, received_at=FIXED_NOW))[0]
    relay = OutboxRelay(database, InMemoryPublisher(), lease_seconds=60)

    first = (await relay.claim_batch(1, now=FIXED_NOW))[0]
    second = (await relay.claim_batch(1, now=FIXED_NOW + timedelta(seconds=61)))[0]

    assert first.id == second.id == message_id
    assert first.claim_id != second.claim_id
    assert second.attempt_count == 2


@pytest.mark.integration
async def test_publish_batch_marks_acknowledged_message_published(database: Database) -> None:
    message_id = (await create_pending_messages(database, count=1, received_at=datetime.now(UTC)))[
        0
    ]

    result = await OutboxRelay(database, InMemoryPublisher(), 60).publish_batch(10)

    row = await load_outbox(database, message_id)
    assert result == PublishBatchResult(claimed=1, published=1, failed=0)
    assert row.state == OutboxState.PUBLISHED
    assert row.provider_message_id == f"in-memory:{message_id}"
    assert row.claim_id is None and row.lease_expires_at is None
    assert row.published_at is not None


@pytest.mark.integration
async def test_publish_failure_releases_message_without_storing_secret(
    database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    relay_logger = logging.getLogger("eva_ai.events.outbox")
    relay_logger.disabled = False
    caplog.set_level(logging.INFO, logger=relay_logger.name)
    message_id = (await create_pending_messages(database, count=1, received_at=datetime.now(UTC)))[
        0
    ]

    result = await OutboxRelay(database, FailingPublisher(), 60).publish_batch(10)

    row = await load_outbox(database, message_id)
    assert result == PublishBatchResult(claimed=1, published=0, failed=1)
    assert row.state == OutboxState.PENDING
    assert row.claim_id is None and row.lease_expires_at is None
    assert row.last_error_type == "RuntimeError"
    assert row.last_error_summary == "operation failed"
    assert "provider-token" not in f"{row.last_error_type}:{row.last_error_summary}"
    assert "provider-token" not in caplog.text
    record = caplog.records[-1]
    assert record.__dict__["outbox_message_id"] == str(message_id)
    assert record.__dict__["outcome"] == "failed"
    assert not hasattr(record, "payload")


@pytest.mark.integration
async def test_publish_batch_continues_after_one_message_fails(database: Database) -> None:
    failed_id, published_id = await create_pending_messages(
        database,
        count=2,
        received_at=datetime.now(UTC),
    )

    result = await OutboxRelay(database, SelectiveFailingPublisher(failed_id), 60).publish_batch(10)

    failed_row = await load_outbox(database, failed_id)
    published_row = await load_outbox(database, published_id)
    assert result == PublishBatchResult(claimed=2, published=1, failed=1)
    assert failed_row.state == OutboxState.PENDING
    assert published_row.state == OutboxState.PUBLISHED
    assert published_row.provider_message_id == f"selective:{published_id}"


@pytest.mark.integration
async def test_publish_batch_continues_when_failed_publish_loses_lease(
    database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    relay_logger = logging.getLogger("eva_ai.events.outbox")
    relay_logger.disabled = False
    caplog.set_level(logging.INFO, logger=relay_logger.name)
    available_at = datetime.now(UTC) - timedelta(seconds=2)
    first_id = (await create_pending_messages(database, count=1, received_at=available_at))[0]
    second_id = (
        await create_pending_messages(
            database,
            count=1,
            received_at=available_at + timedelta(seconds=1),
        )
    )[0]
    reclaiming_relay = OutboxRelay(database, InMemoryPublisher(), lease_seconds=60)
    publisher = ReclaimingFailFirstPublisher(
        reclaiming_relay,
        reclaim_at=datetime.now(UTC) + timedelta(seconds=61),
    )

    result = await OutboxRelay(database, publisher, lease_seconds=60).publish_batch(2)

    first_row = await load_outbox(database, first_id)
    second_row = await load_outbox(database, second_id)
    assert result == PublishBatchResult(claimed=2, published=1, failed=1)
    assert publisher.reclaimed is not None
    assert publisher.reclaimed.id == first_id
    assert first_row.state == OutboxState.PUBLISHING
    assert first_row.claim_id == publisher.reclaimed.claim_id
    assert first_row.attempt_count == 2
    assert first_row.last_error_type is None and first_row.last_error_summary is None
    assert second_row.state == OutboxState.PUBLISHED
    assert second_row.provider_message_id == f"after-stale:{second_id}"
    outcomes = {
        record.__dict__["outbox_message_id"]: record.__dict__["outcome"]
        for record in caplog.records
        if "outcome" in record.__dict__
    }
    assert outcomes == {str(first_id): "stale", str(second_id): "published"}
    assert "provider-token" not in caplog.text


@pytest.mark.integration
async def test_stale_claim_cannot_complete_reclaimed_message(database: Database) -> None:
    await create_pending_messages(database, count=1, received_at=FIXED_NOW)
    relay = OutboxRelay(database, InMemoryPublisher(), 60)
    old = (await relay.claim_batch(1, now=FIXED_NOW))[0]
    current = (await relay.claim_batch(1, now=FIXED_NOW + timedelta(seconds=61)))[0]

    with pytest.raises(StaleClaimError):
        await relay.complete_claim(old.id, old.claim_id, "late-provider-id", FIXED_NOW)

    row = await load_outbox(database, old.id)
    assert row.state == OutboxState.PUBLISHING
    assert row.claim_id == current.claim_id
    assert row.provider_message_id is None


@pytest.mark.integration
async def test_stale_claim_cannot_release_reclaimed_message(database: Database) -> None:
    await create_pending_messages(database, count=1, received_at=FIXED_NOW)
    relay = OutboxRelay(database, InMemoryPublisher(), 60)
    old = (await relay.claim_batch(1, now=FIXED_NOW))[0]
    current = (await relay.claim_batch(1, now=FIXED_NOW + timedelta(seconds=61)))[0]

    with pytest.raises(StaleClaimError):
        await relay.release_claim(old.id, old.claim_id, RuntimeError("provider-token"))

    row = await load_outbox(database, old.id)
    assert row.state == OutboxState.PUBLISHING
    assert row.claim_id == current.claim_id
    assert row.last_error_type is None and row.last_error_summary is None
