from typing import Protocol

from eva_ai.events.types import OutboundMessage


class Publisher(Protocol):
    async def publish(self, message: OutboundMessage) -> str:
        raise NotImplementedError


class InMemoryPublisher:
    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    async def publish(self, message: OutboundMessage) -> str:
        self.messages.append(message)
        return f"in-memory:{message.outbox_message_id}"
