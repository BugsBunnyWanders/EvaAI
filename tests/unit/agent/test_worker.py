from uuid import uuid7

from eva_ai.agent.types import AgentRunRequestedMessage, InvestigationOutcome
from eva_ai.agent.worker import AgentPullBatchResult, AgentPullWorker
from eva_ai.connectors.gmail.contracts import PullMessage


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
    async def process(self, message: AgentRunRequestedMessage) -> InvestigationOutcome:
        return (
            InvestigationOutcome.SUCCEEDED
            if message.schema_version == 1
            else InvestigationOutcome.RETRY
        )


def _message(index: int, *, schema_version: int = 1, valid: bool = True) -> PullMessage:
    envelope = AgentRunRequestedMessage(
        outbox_message_id=uuid7(),
        agent_run_id=uuid7(),
        event_id=uuid7(),
        signal_id=uuid7(),
        situation_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        schema_version=schema_version,
    )
    return PullMessage(
        ack_id=f"ack-{index}",
        message_id=f"message-{index}",
        data=envelope.model_dump_json().encode() if valid else b"invalid",
    )


async def test_worker_acks_terminal_and_poison_but_nacks_retryable() -> None:
    subscriber = Subscriber((_message(1), _message(2, schema_version=2), _message(3, valid=False)))
    worker = AgentPullWorker(subscriber, Handler(), 30)

    result = await worker.run_once()

    assert result == AgentPullBatchResult(3, 2, 1)
    assert subscriber.acknowledged == ("ack-1", "ack-3")
    assert subscriber.negative == ("ack-2",)
