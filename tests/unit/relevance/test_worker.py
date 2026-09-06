from typing import Never
from uuid import uuid7

from eva_ai.connectors.gmail.contracts import PullMessage
from eva_ai.events.processor import EventHandler, ProcessOutcome, ProcessResult, StoredEvent
from eva_ai.events.types import EventAvailableMessage
from eva_ai.relevance.worker import RelevancePullBatchResult, RelevancePullWorker


class Subscriber:
    def __init__(self, messages: tuple[PullMessage, ...]) -> None:
        self.messages = messages
        self.acknowledged: tuple[str, ...] = ()
        self.negative: tuple[str, ...] = ()

    async def pull(self, max_messages: int, timeout_seconds: int) -> tuple[PullMessage, ...]:
        return self.messages

    async def acknowledge(self, ack_ids: tuple[str, ...]) -> None:
        self.acknowledged = ack_ids

    async def negative_acknowledge(self, ack_ids: tuple[str, ...]) -> None:
        self.negative = ack_ids

    async def close(self) -> None:
        return None


class Processor:
    async def process(self, message: EventAvailableMessage, handler: EventHandler) -> ProcessResult:
        return ProcessResult(message.event_id, ProcessOutcome.HANDLED)


class Handler:
    async def prepare(self, event: StoredEvent) -> Never:
        raise AssertionError("processor fake does not invoke handler")


def pull_message(index: int, *, valid: bool = True) -> PullMessage:
    envelope = EventAvailableMessage(
        outbox_message_id=uuid7(),
        event_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        event_type="email.received",
        schema_version=1,
    )
    return PullMessage(
        ack_id=f"ack-{index}",
        message_id=f"message-{index}",
        data=envelope.model_dump_json().encode() if valid else b"not-json",
    )


async def test_worker_acknowledges_handled_and_poison_messages() -> None:
    subscriber = Subscriber((pull_message(1), pull_message(2, valid=False)))
    worker = RelevancePullWorker(subscriber, Processor(), Handler(), 17)

    result = await worker.run_once(3)

    assert result == RelevancePullBatchResult(2, 2, 0)
    assert subscriber.acknowledged == ("ack-1", "ack-2")
    assert subscriber.negative == ()
