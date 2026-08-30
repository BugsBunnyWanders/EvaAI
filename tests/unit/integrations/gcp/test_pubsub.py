import json
from uuid import uuid7

import pytest

from eva_ai.events.types import EventAvailableMessage, OutboundMessage
from eva_ai.integrations.gcp.pubsub import GooglePubSubPublisher


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


class FakeFuture:
    def result(self, timeout: float | None = None) -> str:
        assert timeout is None
        return "provider-message-42"


class FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, dict[str, str]]] = []

    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic: str, data: bytes, **attrs: str) -> FakeFuture:
        self.published.append((topic, data, attrs))
        return FakeFuture()


@pytest.mark.asyncio
async def test_google_publisher_serializes_envelope_and_awaits_ack() -> None:
    client = FakeClient()
    message = outbound_message(destination="eva-events")

    provider_id = await GooglePubSubPublisher("eva-project", client).publish(message)
    topic, data, attrs = client.published[0]

    assert provider_id == "provider-message-42"
    assert topic == "projects/eva-project/topics/eva-events"
    assert json.loads(data) == message.envelope.model_dump(mode="json")
    assert attrs == {
        "message_type": "event.available",
        "event_id": str(message.envelope.event_id),
        "workspace_id": str(message.envelope.workspace_id),
    }
