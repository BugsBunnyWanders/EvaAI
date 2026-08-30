from collections.abc import Mapping
from typing import Never, cast
from uuid import uuid7

import pytest

from eva_ai.config import Settings
from eva_ai.db.session import Database
from eva_ai.events.processor import (
    EventHandler,
    EventProcessor,
    ProcessOutcome,
    ProcessResult,
    StoredEvent,
)
from eva_ai.events.publisher import InMemoryPublisher, Publisher
from eva_ai.events.types import EventAvailableMessage
from eva_ai.worker import (
    build_event_processor,
    build_outbox_relay,
    build_publisher,
    dispatch_event,
)


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


class RecordingOutboxRelay:
    def __init__(
        self,
        database: Database,
        publisher: Publisher,
        lease_seconds: int,
    ) -> None:
        self.database = database
        self.publisher = publisher
        self.lease_seconds = lease_seconds


class RecordingEventProcessor:
    def __init__(self, database: Database, lease_seconds: int) -> None:
        self.database = database
        self.lease_seconds = lease_seconds


def fail_google_publisher_construction(project_id: str) -> Never:
    raise AssertionError(f"Google publisher must not be constructed for {project_id!r}")


def test_local_composition_uses_in_memory_publisher() -> None:
    settings = Settings(_env_file=None)

    publisher = build_publisher(settings, use_google=False)

    assert isinstance(publisher, InMemoryPublisher)


def test_google_composition_requires_project_id() -> None:
    settings = Settings(_env_file=None, pubsub_project_id=None)

    with pytest.raises(ValueError, match="EVA_PUBSUB_PROJECT_ID"):
        build_publisher(settings, use_google=True)


@pytest.mark.parametrize("project_id", ["", "   "])
def test_google_composition_rejects_blank_project_before_adapter_construction(
    project_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, pubsub_project_id=project_id)
    monkeypatch.setattr(
        "eva_ai.worker.GooglePubSubPublisher",
        fail_google_publisher_construction,
    )

    with pytest.raises(ValueError, match="EVA_PUBSUB_PROJECT_ID"):
        build_publisher(settings, use_google=True)


def test_outbox_relay_composition_passes_collaborators_and_outbox_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, outbox_lease_seconds=17, processing_lease_seconds=23)
    database = Database(settings.database_url.get_secret_value())
    publisher = InMemoryPublisher()
    monkeypatch.setattr("eva_ai.worker.OutboxRelay", RecordingOutboxRelay)

    relay = cast(
        RecordingOutboxRelay,
        build_outbox_relay(database, settings, publisher),
    )

    assert relay.database is database
    assert relay.publisher is publisher
    assert relay.lease_seconds == 17


def test_event_processor_composition_passes_database_and_processing_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, outbox_lease_seconds=17, processing_lease_seconds=23)
    database = Database(settings.database_url.get_secret_value())
    monkeypatch.setattr("eva_ai.worker.EventProcessor", RecordingEventProcessor)

    processor = cast(
        RecordingEventProcessor,
        build_event_processor(database, settings),
    )

    assert processor.database is database
    assert processor.lease_seconds == 23


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
