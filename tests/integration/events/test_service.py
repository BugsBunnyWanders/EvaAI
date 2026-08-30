import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid7

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from eva_ai.db import Database
from eva_ai.db.models import Event, EventProcessing, OutboxMessage
from eva_ai.events import EventService, IngestResult
from eva_ai.events.types import NewEvent, OutboxState, PrincipalType, ProcessingStage
from tests.integration.factories import Scope, create_scope


def make_event(
    scope: Scope,
    *,
    user_id: UUID | None = None,
    idempotency_key: str,
) -> NewEvent:
    return NewEvent(
        user_id=user_id or scope.user_id,
        workspace_id=scope.workspace_id,
        source="gmail",
        event_type="email.received",
        idempotency_key=idempotency_key,
        occurred_at=datetime.now(UTC),
        principal_type=PrincipalType.USER,
        payload={"message_id": idempotency_key},
        metadata={"ingested_by": "test"},
    )


async def backbone_counts(database: Database, workspace_id: UUID) -> tuple[int, int, int]:
    async with database.session() as session:
        event_count = await session.scalar(
            select(func.count()).select_from(Event).where(Event.workspace_id == workspace_id)
        )
        processing_count = await session.scalar(
            select(func.count())
            .select_from(EventProcessing)
            .join(Event, Event.id == EventProcessing.event_id)
            .where(Event.workspace_id == workspace_id)
        )
        outbox_count = await session.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .join(Event, Event.id == OutboxMessage.event_id)
            .where(Event.workspace_id == workspace_id)
        )
    return int(event_count or 0), int(processing_count or 0), int(outbox_count or 0)


@pytest.mark.integration
async def test_ingest_creates_event_processing_and_outbox_atomically(
    database: Database,
) -> None:
    scope = await create_scope(database)
    command = make_event(scope, idempotency_key="gmail:atomic-1")

    result = await EventService(database, "eva-events").ingest(command)

    async with database.session() as session:
        event = await session.get(Event, result.event_id)
        processing = await session.get(EventProcessing, result.event_id)
        outbox = await session.scalar(
            select(OutboxMessage).where(OutboxMessage.event_id == result.event_id)
        )
    assert result.created is True
    assert event is not None and event.payload == {"message_id": "gmail:atomic-1"}
    assert event.event_metadata == {"ingested_by": "test"}
    assert processing is not None and processing.stage == ProcessingStage.RECEIVED
    assert outbox is not None and outbox.state == OutboxState.PENDING
    assert outbox.destination == "eva-events"
    assert outbox.payload["event_id"] == str(result.event_id)


@pytest.mark.integration
async def test_duplicate_ingest_returns_existing_event_without_more_children(
    database: Database,
) -> None:
    scope = await create_scope(database)
    command = make_event(scope, idempotency_key="same-key")
    service = EventService(database, "eva-events")

    first_result = await service.ingest(command)
    second_result = await service.ingest(command.model_copy(update={"id": uuid7()}))

    assert second_result == IngestResult(event_id=first_result.event_id, created=False)
    assert await backbone_counts(database, scope.workspace_id) == (1, 1, 1)


@pytest.mark.integration
async def test_concurrent_duplicate_ingest_creates_one_backbone(database: Database) -> None:
    scope = await create_scope(database)
    command = make_event(scope, idempotency_key="concurrent-key")

    first, second = await asyncio.gather(
        EventService(database, "eva-events").ingest(command),
        EventService(database, "eva-events").ingest(command.model_copy(update={"id": uuid7()})),
    )

    assert first.event_id == second.event_id
    assert sorted([first.created, second.created]) == [False, True]
    assert await backbone_counts(database, scope.workspace_id) == (1, 1, 1)


@pytest.mark.integration
async def test_invalid_scope_rolls_back_the_whole_backbone(database: Database) -> None:
    scope = await create_scope(database)
    command = make_event(scope, user_id=uuid7(), idempotency_key="invalid-scope")

    with pytest.raises(IntegrityError):
        await EventService(database, "eva-events").ingest(command)

    assert await backbone_counts(database, scope.workspace_id) == (0, 0, 0)
