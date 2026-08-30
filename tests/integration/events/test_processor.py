import logging
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import pytest
from sqlalchemy import select

from eva_ai.db import Database
from eva_ai.db.models import EventProcessing, OutboxMessage
from eva_ai.events import (
    EventAvailableMessage,
    EventProcessor,
    EventService,
    ProcessOutcome,
    ScopeMismatchError,
    StaleClaimError,
    StoredEvent,
    UnknownEventError,
)
from eva_ai.events.types import NewEvent, PrincipalType, ProcessingStage
from tests.integration.factories import create_scope

FIXED_NOW = datetime(2030, 1, 1, tzinfo=UTC)


class RecordingHandler:
    def __init__(self) -> None:
        self.events: list[StoredEvent] = []

    async def handle(self, event: StoredEvent) -> None:
        self.events.append(event)


class LockingHandler:
    def __init__(self, database: Database) -> None:
        self._database = database
        self.claim_id: UUID | None = None
        self.attempt_count: int | None = None

    async def handle(self, event: StoredEvent) -> None:
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(EventProcessing)
                    .where(EventProcessing.event_id == event.id)
                    .with_for_update(nowait=True)
                )
                assert row is not None
                self.claim_id = row.claim_id
                self.attempt_count = row.attempt_count


class FailingHandler:
    async def handle(self, event: StoredEvent) -> None:
        raise RuntimeError(f"handler-token payload-secret {event.id}")


async def ingest_message(database: Database) -> EventAvailableMessage:
    scope = await create_scope(database)
    marker = uuid7()
    result = await EventService(database, "eva-events").ingest(
        NewEvent(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            source="gmail",
            event_type="email.received",
            idempotency_key=f"processor-{marker}",
            occurred_at=FIXED_NOW,
            received_at=FIXED_NOW,
            principal_type=PrincipalType.USER,
            payload={"message_id": str(marker)},
        )
    )
    async with database.session() as session:
        payload = await session.scalar(
            select(OutboxMessage.payload).where(OutboxMessage.event_id == result.event_id)
        )
    assert payload is not None
    return EventAvailableMessage.model_validate(payload)


async def load_processing(database: Database, event_id: UUID) -> EventProcessing:
    async with database.session() as session:
        row = await session.get(EventProcessing, event_id)
    assert row is not None
    return row


async def install_processing_claim(
    database: Database,
    event_id: UUID,
    lease_expires_at: datetime,
    *,
    claim_id: UUID | None = None,
    attempt_count: int = 1,
    stage: ProcessingStage = ProcessingStage.RECEIVED,
) -> UUID:
    installed_claim_id = claim_id or uuid7()
    async with database.session() as session:
        async with session.begin():
            row = await session.get(EventProcessing, event_id)
            assert row is not None
            row.stage = stage
            row.attempt_count = attempt_count
            row.claim_id = installed_claim_id
            row.lease_expires_at = lease_expires_at
    return installed_claim_id


@pytest.mark.integration
async def test_processor_handles_event_and_redelivery_is_idempotent(
    database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    processor_logger = logging.getLogger("eva_ai.events.processor")
    processor_logger.disabled = False
    caplog.set_level(logging.INFO, logger=processor_logger.name)
    message = await ingest_message(database)
    handler = RecordingHandler()
    processor = EventProcessor(database, lease_seconds=300)

    first = await processor.process(message, handler, FIXED_NOW)
    second = await processor.process(message, handler, FIXED_NOW)

    row = await load_processing(database, message.event_id)
    assert first.outcome == ProcessOutcome.HANDLED
    assert second.outcome == ProcessOutcome.ALREADY_HANDLED
    assert [event.id for event in handler.events] == [message.event_id]
    assert handler.events[0].payload["message_id"] is not None
    immutable_field = "source"
    with pytest.raises(FrozenInstanceError):
        setattr(handler.events[0], immutable_field, "changed")
    assert row.stage == ProcessingStage.HANDLED
    assert row.attempt_count == 1
    assert row.processed_at is not None
    assert row.claim_id is None and row.lease_expires_at is None
    records = [record for record in caplog.records if record.name == processor_logger.name]
    assert [record.__dict__["outcome"] for record in records] == [
        "handled",
        "already_handled",
    ]
    for record in records:
        assert record.__dict__["event_id"] == str(message.event_id)
        assert record.__dict__["user_id"] == str(message.user_id)
        assert record.__dict__["workspace_id"] == str(message.workspace_id)
        assert "claim_id" in record.__dict__
        assert not hasattr(record, "payload")


@pytest.mark.integration
async def test_processor_rejects_unknown_event_without_handler(database: Database) -> None:
    message = (await ingest_message(database)).model_copy(update={"event_id": uuid7()})
    handler = RecordingHandler()

    with pytest.raises(UnknownEventError):
        await EventProcessor(database, 300).process(message, handler, FIXED_NOW)

    assert handler.events == []


@pytest.mark.integration
@pytest.mark.parametrize("field", ["user_id", "workspace_id"])
async def test_processor_rejects_scope_mismatch_without_handler(
    database: Database,
    field: str,
) -> None:
    message = await ingest_message(database)
    handler = RecordingHandler()

    with pytest.raises(ScopeMismatchError):
        await EventProcessor(database, 300).process(
            message.model_copy(update={field: uuid7()}),
            handler,
            FIXED_NOW,
        )

    assert handler.events == []
    assert (await load_processing(database, message.event_id)).attempt_count == 0


@pytest.mark.integration
async def test_handler_runs_after_claim_transaction_and_session_close(database: Database) -> None:
    message = await ingest_message(database)
    handler = LockingHandler(database)

    result = await EventProcessor(database, 300).process(message, handler, FIXED_NOW)

    assert result.outcome == ProcessOutcome.HANDLED
    assert handler.claim_id is not None
    assert handler.claim_id.version == 7
    assert handler.attempt_count == 1


@pytest.mark.integration
async def test_active_processing_claim_returns_retryable_busy(database: Database) -> None:
    message = await ingest_message(database)
    active_claim_id = await install_processing_claim(
        database,
        message.event_id,
        FIXED_NOW + timedelta(seconds=30),
    )
    handler = RecordingHandler()

    result = await EventProcessor(database, 300).process(message, handler, FIXED_NOW)

    row = await load_processing(database, message.event_id)
    assert result.outcome == ProcessOutcome.BUSY
    assert handler.events == []
    assert row.claim_id == active_claim_id
    assert row.lease_expires_at == FIXED_NOW + timedelta(seconds=30)
    assert row.attempt_count == 1


@pytest.mark.integration
async def test_expired_processing_claim_is_reclaimed_with_fresh_uuid7(
    database: Database,
) -> None:
    message = await ingest_message(database)
    expired_claim_id = await install_processing_claim(
        database,
        message.event_id,
        FIXED_NOW - timedelta(seconds=1),
    )
    handler = LockingHandler(database)

    result = await EventProcessor(database, 300).process(message, handler, FIXED_NOW)

    row = await load_processing(database, message.event_id)
    assert result.outcome == ProcessOutcome.HANDLED
    assert handler.claim_id is not None
    assert handler.claim_id != expired_claim_id
    assert handler.claim_id.version == 7
    assert handler.attempt_count == 2
    assert row.stage == ProcessingStage.HANDLED
    assert row.attempt_count == 2


@pytest.mark.integration
async def test_stale_claim_cannot_complete_reclaimed_event(database: Database) -> None:
    message = await ingest_message(database)
    stale_claim_id = uuid7()
    current_claim_id = await install_processing_claim(
        database,
        message.event_id,
        FIXED_NOW + timedelta(seconds=300),
        attempt_count=2,
    )

    with pytest.raises(StaleClaimError):
        await EventProcessor(database, 300)._complete(message.event_id, stale_claim_id)

    row = await load_processing(database, message.event_id)
    assert row.stage == ProcessingStage.RECEIVED
    assert row.claim_id == current_claim_id
    assert row.attempt_count == 2
    assert row.processed_at is None


@pytest.mark.integration
async def test_stale_claim_cannot_release_reclaimed_event(database: Database) -> None:
    message = await ingest_message(database)
    stale_claim_id = uuid7()
    current_claim_id = await install_processing_claim(
        database,
        message.event_id,
        FIXED_NOW + timedelta(seconds=300),
        attempt_count=2,
        stage=ProcessingStage.CLASSIFIED,
    )

    with pytest.raises(StaleClaimError):
        await EventProcessor(database, 300)._release(
            message.event_id,
            stale_claim_id,
            RuntimeError("handler-token"),
        )

    row = await load_processing(database, message.event_id)
    assert row.stage == ProcessingStage.CLASSIFIED
    assert row.claim_id == current_claim_id
    assert row.attempt_count == 2
    assert row.last_error_type is None and row.last_error_summary is None


@pytest.mark.integration
async def test_handler_failure_is_sanitized_and_next_delivery_can_succeed(
    database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    processor_logger = logging.getLogger("eva_ai.events.processor")
    processor_logger.disabled = False
    caplog.set_level(logging.INFO, logger=processor_logger.name)
    message = await ingest_message(database)
    processor = EventProcessor(database, 300)

    with pytest.raises(RuntimeError, match="handler-token"):
        await processor.process(message, FailingHandler(), FIXED_NOW)

    failed = await load_processing(database, message.event_id)
    assert failed.stage == ProcessingStage.RECEIVED
    assert failed.claim_id is None and failed.lease_expires_at is None
    assert failed.attempt_count == 1
    assert failed.last_error_type == "RuntimeError"
    assert failed.last_error_summary == "operation failed"

    result = await processor.process(message, RecordingHandler(), FIXED_NOW)

    handled = await load_processing(database, message.event_id)
    assert result.outcome == ProcessOutcome.HANDLED
    assert handled.stage == ProcessingStage.HANDLED
    assert handled.attempt_count == 2
    assert handled.last_error_type is None and handled.last_error_summary is None
    assert "handler-token" not in caplog.text
    assert "payload-secret" not in caplog.text
    records = [record for record in caplog.records if record.name == processor_logger.name]
    assert [record.__dict__["outcome"] for record in records] == ["failed", "handled"]
    assert all(not hasattr(record, "payload") for record in records)
    assert all(not hasattr(record, "error") for record in records)
