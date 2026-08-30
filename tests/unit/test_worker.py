from collections.abc import Mapping
from typing import cast
from uuid import uuid7

import pytest

from eva_ai.config import Settings
from eva_ai.events.processor import (
    EventHandler,
    EventProcessor,
    ProcessOutcome,
    ProcessResult,
    StoredEvent,
)
from eva_ai.events.publisher import InMemoryPublisher
from eva_ai.events.types import EventAvailableMessage
from eva_ai.worker import build_publisher, dispatch_event


class RecordingProcessor:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.messages: list[EventAvailableMessage] = []
        self.handlers: list[EventHandler] = []

    async def process(
        self,
        message: EventAvailableMessage,
        handler: EventHandler,
    ) -> ProcessResult:
        self.messages.append(message)
        self.handlers.append(handler)
        return self.result


class UnexpectedHandler:
    async def handle(self, event: StoredEvent) -> None:
        raise AssertionError(f"worker unexpectedly invoked handler for {event.id}")


def test_local_composition_uses_in_memory_publisher() -> None:
    settings = Settings(_env_file=None)

    publisher = build_publisher(settings, use_google=False)

    assert isinstance(publisher, InMemoryPublisher)


def test_google_composition_requires_project_id() -> None:
    settings = Settings(_env_file=None, pubsub_project_id=None)

    with pytest.raises(ValueError, match="EVA_PUBSUB_PROJECT_ID"):
        build_publisher(settings, use_google=True)


@pytest.mark.parametrize("raw_format", ["mapping", "bytes", "string"])
@pytest.mark.asyncio
async def test_dispatch_validates_raw_message_before_one_processing_call(
    raw_format: str,
) -> None:
    message = EventAvailableMessage(
        outbox_message_id=uuid7(),
        event_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        event_type="test.created",
        schema_version=1,
    )
    raw_message: Mapping[str, object] | bytes | str
    if raw_format == "mapping":
        raw_message = message.model_dump(mode="json")
    elif raw_format == "bytes":
        raw_message = message.model_dump_json().encode("utf-8")
    else:
        raw_message = message.model_dump_json()
    expected = ProcessResult(message.event_id, ProcessOutcome.HANDLED)
    processor = RecordingProcessor(expected)
    handler = UnexpectedHandler()

    result = await dispatch_event(cast(EventProcessor, processor), raw_message, handler)

    assert processor.messages == [message]
    assert processor.handlers == [handler]
    assert result == expected
