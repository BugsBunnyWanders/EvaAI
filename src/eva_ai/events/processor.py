import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID, uuid7

from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from eva_ai.db.models import Event, EventProcessing
from eva_ai.db.session import Database
from eva_ai.events.errors import (
    ScopeMismatchError,
    StaleClaimError,
    UnknownEventError,
    sanitize_error,
)
from eva_ai.events.types import EventAvailableMessage, ProcessingStage

_LOGGER = logging.getLogger(__name__)


class ProcessOutcome(StrEnum):
    HANDLED = "HANDLED"
    ALREADY_HANDLED = "ALREADY_HANDLED"
    BUSY = "BUSY"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    event_id: UUID
    outcome: ProcessOutcome


@dataclass(frozen=True, slots=True)
class StoredEvent:
    id: UUID
    user_id: UUID
    workspace_id: UUID
    source: str
    event_type: str
    payload: dict[str, JsonValue]
    schema_version: int


class EventHandler(Protocol):
    async def handle(self, event: StoredEvent) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _ClaimedEvent:
    event: StoredEvent
    claim_id: UUID


class EventProcessor:
    def __init__(self, database: Database, lease_seconds: int) -> None:
        self._database = database
        self._lease_seconds = lease_seconds

    async def _claim(
        self,
        message: EventAvailableMessage,
        now: datetime,
    ) -> _ClaimedEvent | ProcessResult:
        statement = (
            select(Event, EventProcessing)
            .join(EventProcessing, EventProcessing.event_id == Event.id)
            .where(Event.id == message.event_id)
            .with_for_update(of=EventProcessing)
        )
        async with self._database.session() as session:
            async with session.begin():
                row = (await session.execute(statement)).one_or_none()
                if row is None:
                    raise UnknownEventError("event does not exist")

                event, processing = row
                if event.user_id != message.user_id or event.workspace_id != message.workspace_id:
                    raise ScopeMismatchError("event scope does not match message")
                if processing.stage == ProcessingStage.HANDLED:
                    self._log(message, None, "already_handled")
                    return ProcessResult(event.id, ProcessOutcome.ALREADY_HANDLED)
                if (
                    processing.claim_id is not None
                    and processing.lease_expires_at is not None
                    and processing.lease_expires_at > now
                ):
                    self._log(message, processing.claim_id, "busy")
                    return ProcessResult(event.id, ProcessOutcome.BUSY)

                claim_id = uuid7()
                processing.claim_id = claim_id
                processing.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
                processing.attempt_count += 1
                processing.last_error_type = None
                processing.last_error_summary = None
                stored_event = StoredEvent(
                    id=event.id,
                    user_id=event.user_id,
                    workspace_id=event.workspace_id,
                    source=event.source,
                    event_type=event.event_type,
                    payload=dict(event.payload),
                    schema_version=event.schema_version,
                )

        # Handler code receives detached data only after the claim transaction is committed.
        return _ClaimedEvent(stored_event, claim_id)

    async def _complete(self, event_id: UUID, claim_id: UUID) -> None:
        statement = (
            update(EventProcessing)
            .where(
                EventProcessing.event_id == event_id,
                EventProcessing.claim_id == claim_id,
                EventProcessing.stage != ProcessingStage.HANDLED,
            )
            .values(
                stage=ProcessingStage.HANDLED,
                processed_at=datetime.now(UTC),
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
                    raise StaleClaimError("event processing claim is no longer current")

    async def _release(
        self,
        event_id: UUID,
        claim_id: UUID,
        error: BaseException,
    ) -> None:
        stored_error = sanitize_error(error)
        statement = (
            update(EventProcessing)
            .where(
                EventProcessing.event_id == event_id,
                EventProcessing.claim_id == claim_id,
                EventProcessing.stage != ProcessingStage.HANDLED,
            )
            .values(
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
                    raise StaleClaimError("event processing claim is no longer current")

    @staticmethod
    def _log(
        message: EventAvailableMessage,
        claim_id: UUID | None,
        outcome: str,
    ) -> None:
        _LOGGER.info(
            "event processing finished",
            extra={
                "event_id": str(message.event_id),
                "user_id": str(message.user_id),
                "workspace_id": str(message.workspace_id),
                "claim_id": str(claim_id) if claim_id is not None else None,
                "outcome": outcome,
            },
        )

    async def process(
        self,
        message: EventAvailableMessage,
        handler: EventHandler,
        now: datetime | None = None,
    ) -> ProcessResult:
        claim = await self._claim(message, now or datetime.now(UTC))
        if isinstance(claim, ProcessResult):
            return claim
        try:
            await handler.handle(claim.event)
        except Exception as error:
            await self._release(claim.event.id, claim.claim_id, error)
            self._log(message, claim.claim_id, "failed")
            raise
        await self._complete(claim.event.id, claim.claim_id)
        self._log(message, claim.claim_id, "handled")
        return ProcessResult(claim.event.id, ProcessOutcome.HANDLED)
