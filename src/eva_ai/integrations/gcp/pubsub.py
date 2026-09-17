import asyncio
from typing import Protocol, cast

import google.cloud.pubsub_v1 as pubsub_v1  # type: ignore[import-untyped]

from eva_ai.events.types import OutboundMessage


class PublishFuture(Protocol):
    def result(self, timeout: float | None = None) -> str:
        raise NotImplementedError


class PubSubClient(Protocol):
    def topic_path(self, project: str, topic: str) -> str:
        raise NotImplementedError

    def publish(self, topic: str, data: bytes, **attrs: str) -> PublishFuture:
        raise NotImplementedError


class GooglePubSubPublisher:
    def __init__(self, project_id: str, client: PubSubClient | None = None) -> None:
        self._project_id = project_id
        self._client = (
            client if client is not None else cast(PubSubClient, pubsub_v1.PublisherClient())
        )

    async def publish(self, message: OutboundMessage) -> str:
        topic = self._client.topic_path(self._project_id, message.destination)
        data = message.envelope.model_dump_json().encode("utf-8")
        attributes = {
            "message_type": message.envelope.message_type,
            "workspace_id": str(message.envelope.workspace_id),
        }
        # Not every outbox envelope represents an Event; action dispatch is keyed by action_id.
        event_id = getattr(message.envelope, "event_id", None)
        if event_id is not None:
            attributes["event_id"] = str(event_id)
        future = self._client.publish(topic, data, **attributes)
        # Pub/Sub returns a blocking Future, so wait in a thread instead of blocking asyncio.
        return await asyncio.to_thread(future.result)
