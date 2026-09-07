from uuid import uuid7

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from eva_ai.db.models import Event, EventProcessing, OutboxMessage
from eva_ai.db.session import Database
from eva_ai.events.errors import ScopeMismatchError
from eva_ai.events.service import IngestResult
from eva_ai.events.types import NewEvent, OutboxState, ProcessingStage
from eva_ai.telegram.types import TelegramTurnRequestedMessage


class TelegramEventService:
    """Persists a Telegram Event and routes it away from Gmail relevance processing."""

    def __init__(self, database: Database, destination: str) -> None:
        self._database = database
        self._destination = destination

    async def ingest(self, command: NewEvent) -> IngestResult:
        async with self._database.session() as session:
            async with session.begin():
                statement = (
                    insert(Event)
                    .values(
                        id=command.id,
                        user_id=command.user_id,
                        workspace_id=command.workspace_id,
                        source=command.source,
                        event_type=command.event_type,
                        external_id=command.external_id,
                        idempotency_key=command.idempotency_key,
                        occurred_at=command.occurred_at,
                        received_at=command.received_at,
                        principal_type=command.principal_type,
                        principal_id=command.principal_id,
                        actor=command.actor,
                        subject=command.subject,
                        payload=command.payload,
                        event_metadata=command.metadata,
                        correlation_keys=command.correlation_keys,
                        schema_version=command.schema_version,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[Event.workspace_id, Event.idempotency_key]
                    )
                    .returning(Event.id)
                )
                event_id = (await session.execute(statement)).scalar_one_or_none()
                if event_id is None:
                    existing = (
                        await session.execute(
                            select(Event.id, Event.user_id).where(
                                Event.workspace_id == command.workspace_id,
                                Event.idempotency_key == command.idempotency_key,
                            )
                        )
                    ).one_or_none()
                    if existing is None:
                        raise RuntimeError("conflicting Telegram event was not visible")
                    existing_id, existing_user_id = existing
                    if existing_user_id != command.user_id:
                        raise ScopeMismatchError("event user does not match persisted owner")
                    return IngestResult(event_id=existing_id, created=False)

                outbox_id = uuid7()
                envelope = TelegramTurnRequestedMessage(
                    outbox_message_id=outbox_id,
                    event_id=event_id,
                    user_id=command.user_id,
                    workspace_id=command.workspace_id,
                )
                session.add(EventProcessing(event_id=event_id, stage=ProcessingStage.RECEIVED))
                session.add(
                    OutboxMessage(
                        id=outbox_id,
                        event_id=event_id,
                        destination=self._destination,
                        message_type=envelope.message_type,
                        schema_version=envelope.schema_version,
                        payload=envelope.model_dump(mode="json"),
                        state=OutboxState.PENDING,
                        available_at=command.received_at,
                    )
                )
                return IngestResult(event_id=event_id, created=True)
