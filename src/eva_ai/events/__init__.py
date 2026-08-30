from typing import TYPE_CHECKING

from eva_ai.events.errors import (
    BackboneError,
    ScopeMismatchError,
    StaleClaimError,
    StoredError,
    UnknownEventError,
    sanitize_error,
)
from eva_ai.events.types import (
    EventAvailableMessage,
    NewEvent,
    OutboundMessage,
    OutboxState,
    PrincipalType,
    ProcessingStage,
)

if TYPE_CHECKING:
    from eva_ai.events.outbox import ClaimedOutboxMessage, OutboxRelay, PublishBatchResult
    from eva_ai.events.service import EventService, IngestResult

__all__ = [
    "BackboneError",
    "ClaimedOutboxMessage",
    "EventAvailableMessage",
    "EventService",
    "IngestResult",
    "NewEvent",
    "OutboundMessage",
    "OutboxRelay",
    "OutboxState",
    "PrincipalType",
    "ProcessingStage",
    "PublishBatchResult",
    "ScopeMismatchError",
    "StaleClaimError",
    "StoredError",
    "UnknownEventError",
    "sanitize_error",
]


def __getattr__(name: str) -> object:
    if name in {"ClaimedOutboxMessage", "OutboxRelay", "PublishBatchResult"}:
        from eva_ai.events.outbox import ClaimedOutboxMessage, OutboxRelay, PublishBatchResult

        return {
            "ClaimedOutboxMessage": ClaimedOutboxMessage,
            "OutboxRelay": OutboxRelay,
            "PublishBatchResult": PublishBatchResult,
        }[name]
    if name == "EventService":
        from eva_ai.events.service import EventService

        return EventService
    if name == "IngestResult":
        from eva_ai.events.service import IngestResult

        return IngestResult
    raise AttributeError(name)
