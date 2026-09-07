from uuid import uuid7

from eva_ai.connectors.gmail.contracts import PullMessage
from eva_ai.conversation.types import ConversationOutcome
from eva_ai.conversation.worker import ConversationPullWorker
from eva_ai.telegram.types import TelegramTurnRequestedMessage


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


class Handler:
    async def process(self, message: TelegramTurnRequestedMessage) -> ConversationOutcome:
        return (
            ConversationOutcome.SUCCEEDED
            if message.schema_version == 1
            else ConversationOutcome.RETRY
        )


def _message(index: int, *, version: int = 1, valid: bool = True) -> PullMessage:
    envelope = TelegramTurnRequestedMessage(
        outbox_message_id=uuid7(),
        event_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        schema_version=version,
    )
    return PullMessage(
        ack_id=f"ack-{index}",
        message_id=f"message-{index}",
        data=envelope.model_dump_json().encode() if valid else b"invalid",
    )


async def test_worker_acks_success_and_poison_but_nacks_retry() -> None:
    subscriber = Subscriber((_message(1), _message(2, version=2), _message(3, valid=False)))
    worker = ConversationPullWorker(subscriber, Handler(), 30)

    result = await worker.run_once()

    assert result.pulled == 3
    assert subscriber.acknowledged == ("ack-1", "ack-3")
    assert subscriber.negative == ("ack-2",)
