from uuid import uuid7

import pytest

from eva_ai.events.publisher import InMemoryPublisher
from eva_ai.events.types import EventAvailableMessage, OutboundMessage


def outbound_message(*, destination: str = "eva-events") -> OutboundMessage:
    return OutboundMessage(
        outbox_message_id=uuid7(),
        destination=destination,
        envelope=EventAvailableMessage(
            outbox_message_id=uuid7(),
            event_id=uuid7(),
            user_id=uuid7(),
            workspace_id=uuid7(),
            event_type="email.received",
            schema_version=1,
        ),
    )


@pytest.mark.asyncio
async def test_in_memory_publisher_records_messages_and_returns_stable_id() -> None:
    message = outbound_message()
    publisher = InMemoryPublisher()

    assert await publisher.publish(message) == f"in-memory:{message.outbox_message_id}"
    assert publisher.messages == [message]
