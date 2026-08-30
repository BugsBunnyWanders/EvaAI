from collections.abc import Mapping

from eva_ai.config import Settings
from eva_ai.db.session import Database
from eva_ai.events.outbox import OutboxRelay
from eva_ai.events.processor import EventHandler, EventProcessor, ProcessResult
from eva_ai.events.publisher import InMemoryPublisher, Publisher
from eva_ai.events.types import EventAvailableMessage
from eva_ai.integrations.gcp.pubsub import GooglePubSubPublisher


def build_publisher(settings: Settings, *, use_google: bool) -> Publisher:
    if not use_google:
        return InMemoryPublisher()
    if settings.pubsub_project_id is None:
        raise ValueError("EVA_PUBSUB_PROJECT_ID is required for Google Pub/Sub")
    return GooglePubSubPublisher(settings.pubsub_project_id)


def build_outbox_relay(
    database: Database,
    settings: Settings,
    publisher: Publisher,
) -> OutboxRelay:
    return OutboxRelay(database, publisher, settings.outbox_lease_seconds)


def build_event_processor(database: Database, settings: Settings) -> EventProcessor:
    return EventProcessor(database, settings.processing_lease_seconds)


async def dispatch_event(
    processor: EventProcessor,
    raw_message: Mapping[str, object] | bytes | str,
    handler: EventHandler,
) -> ProcessResult:
    if isinstance(raw_message, bytes | str):
        message = EventAvailableMessage.model_validate_json(raw_message)
    else:
        message = EventAvailableMessage.model_validate(raw_message)
    return await processor.process(message, handler)
