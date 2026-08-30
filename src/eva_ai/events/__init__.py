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
    from eva_ai.events.processor import (
        EventHandler,
        EventProcessor,
        ProcessOutcome,
        ProcessResult,
        StoredEvent,
    )
    from eva_ai.events.service import EventService, IngestResult

__all__ = [
    "BackboneError",
    "ClaimedOutboxMessage",
    "EventAvailableMessage",
    "EventHandler",
    "EventProcessor",
    "EventService",
    "IngestResult",
    "NewEvent",
    "OutboundMessage",
    "OutboxRelay",
    "OutboxState",
    "PrincipalType",
    "ProcessOutcome",
    "ProcessResult",
    "ProcessingStage",
    "PublishBatchResult",
    "ScopeMismatchError",
    "StaleClaimError",
    "StoredEvent",
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
    if name in {
        "EventHandler",
        "EventProcessor",
        "ProcessOutcome",
        "ProcessResult",
        "StoredEvent",
    }:
        from eva_ai.events.processor import (
            EventHandler,
            EventProcessor,
            ProcessOutcome,
            ProcessResult,
            StoredEvent,
        )

        return {
            "EventHandler": EventHandler,
            "EventProcessor": EventProcessor,
            "ProcessOutcome": ProcessOutcome,
            "ProcessResult": ProcessResult,
            "StoredEvent": StoredEvent,
        }[name]
    if name == "EventService":
        from eva_ai.events.service import EventService

        return EventService
    if name == "IngestResult":
        from eva_ai.events.service import IngestResult

        return IngestResult
    raise AttributeError(name)
