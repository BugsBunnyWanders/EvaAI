from datetime import UTC, datetime
from uuid import UUID, uuid7

from eva_ai.actions.dispatcher import ActionDispatchPullWorker, ActionDispatchPullWorkerResult
from eva_ai.actions.types import (
    ActionExecutionRequestedMessage,
    ActionRecord,
    ActionStatus,
    ActionTaskRequest,
    GmailActionCapability,
)
from eva_ai.connectors.gmail.contracts import PullMessage

NOW = datetime(2030, 1, 1, tzinfo=UTC)


class Subscriber:
    def __init__(self, messages: tuple[PullMessage, ...]) -> None:
        self.messages = messages
        self.acknowledged: tuple[str, ...] = ()
        self.negative: tuple[str, ...] = ()

    async def pull(self, max_messages: int, timeout_seconds: int) -> tuple[PullMessage, ...]:
        del max_messages, timeout_seconds
        return self.messages

    async def acknowledge(self, ack_ids: tuple[str, ...]) -> None:
        self.acknowledged = ack_ids

    async def negative_acknowledge(self, ack_ids: tuple[str, ...]) -> None:
        self.negative = ack_ids

    async def close(self) -> None:
        return None


class Store:
    def __init__(self, action: ActionRecord | None) -> None:
        self.action = action
        self.requested: list[UUID] = []
        self.task_names: list[tuple[UUID, UUID, UUID, str]] = []

    async def get_action(self, action_id: UUID) -> ActionRecord | None:
        self.requested.append(action_id)
        return self.action

    async def record_cloud_task_name(
        self,
        *,
        action_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        cloud_task_name: str,
    ) -> bool:
        self.task_names.append((action_id, user_id, workspace_id, cloud_task_name))
        return True


class Enqueuer:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[ActionTaskRequest, GmailActionCapability]] = []

    async def enqueue(
        self,
        request: ActionTaskRequest,
        capability: GmailActionCapability,
    ) -> str:
        self.calls.append((request, capability))
        if self.error is not None:
            raise self.error
        return f"tasks/{request.action_id}"


def _action(
    action_id: UUID,
    user_id: UUID,
    workspace_id: UUID,
    *,
    status: ActionStatus = ActionStatus.QUEUED,
) -> ActionRecord:
    return ActionRecord(
        id=action_id,
        proposal_id=uuid7(),
        event_id=uuid7(),
        user_id=user_id,
        workspace_id=workspace_id,
        capability=GmailActionCapability.CREATE_DRAFT,
        idempotency_key=f"action:{action_id}",
        cloud_task_name=None,
        status=status,
        claim_id=None,
        lease_expires_at=None,
        attempt_count=0,
        provider_call_started_at=None,
        failure_code=None,
        failure_summary=None,
        created_at=NOW,
        started_at=None,
        finished_at=None,
    )


def _message(
    index: int,
    *,
    action_id: UUID | None = None,
    user_id: UUID | None = None,
    workspace_id: UUID | None = None,
) -> tuple[PullMessage, ActionExecutionRequestedMessage]:
    envelope = ActionExecutionRequestedMessage(
        outbox_message_id=uuid7(),
        action_id=action_id or uuid7(),
        user_id=user_id or uuid7(),
        workspace_id=workspace_id or uuid7(),
    )
    return (
        PullMessage(
            ack_id=f"ack-{index}",
            message_id=f"message-{index}",
            data=envelope.model_dump_json().encode(),
        ),
        envelope,
    )


async def test_successful_enqueue_is_acknowledged_with_opaque_task_body() -> None:
    message, envelope = _message(1)
    store = Store(_action(envelope.action_id, envelope.user_id, envelope.workspace_id))
    enqueuer = Enqueuer()
    subscriber = Subscriber((message,))
    worker = ActionDispatchPullWorker(subscriber, store, enqueuer, pull_timeout_seconds=17)

    result = await worker.run_once()

    assert result == ActionDispatchPullWorkerResult(1, 1, 0)
    assert subscriber.acknowledged == ("ack-1",)
    assert subscriber.negative == ()
    assert enqueuer.calls == [
        (
            ActionTaskRequest(action_id=envelope.action_id),
            GmailActionCapability.CREATE_DRAFT,
        )
    ]
    assert store.task_names == [
        (
            envelope.action_id,
            envelope.user_id,
            envelope.workspace_id,
            f"tasks/{envelope.action_id}",
        )
    ]


async def test_malformed_wrong_type_scope_invalid_and_terminal_are_safely_acknowledged() -> None:
    valid, envelope = _message(1)
    wrong_type = valid.data.replace(b"action.execution.requested", b"event.available")
    malformed = PullMessage("ack-2", "message-2", b"not-json")
    invalid_type = PullMessage("ack-3", "message-3", wrong_type)
    scope_mismatched = _action(envelope.action_id, uuid7(), envelope.workspace_id)
    subscriber = Subscriber((malformed, invalid_type, valid))
    enqueuer = Enqueuer()
    worker = ActionDispatchPullWorker(
        subscriber,
        Store(scope_mismatched),
        enqueuer,
        pull_timeout_seconds=17,
    )

    result = await worker.run_once()

    assert result == ActionDispatchPullWorkerResult(3, 3, 0)
    assert subscriber.acknowledged == ("ack-2", "ack-3", "ack-1")
    assert enqueuer.calls == []


async def test_missing_and_terminal_actions_do_not_enqueue() -> None:
    missing_message, _ = _message(1)
    terminal_message, envelope = _message(2)
    terminal = _action(
        envelope.action_id,
        envelope.user_id,
        envelope.workspace_id,
        status=ActionStatus.SUCCEEDED,
    )
    subscriber = Subscriber((missing_message, terminal_message))
    enqueuer = Enqueuer()

    missing_worker = ActionDispatchPullWorker(
        Subscriber((missing_message,)), Store(None), enqueuer, pull_timeout_seconds=17
    )
    missing_result = await missing_worker.run_once()
    terminal_worker = ActionDispatchPullWorker(
        Subscriber((terminal_message,)), Store(terminal), enqueuer, pull_timeout_seconds=17
    )
    terminal_result = await terminal_worker.run_once()

    assert subscriber.acknowledged == ()
    assert missing_result == ActionDispatchPullWorkerResult(1, 1, 0)
    assert terminal_result == ActionDispatchPullWorkerResult(1, 1, 0)
    assert enqueuer.calls == []


async def test_transient_enqueue_failure_is_negative_acknowledged() -> None:
    message, envelope = _message(1)
    subscriber = Subscriber((message,))
    enqueuer = Enqueuer(error=RuntimeError("transient"))
    worker = ActionDispatchPullWorker(
        subscriber,
        Store(_action(envelope.action_id, envelope.user_id, envelope.workspace_id)),
        enqueuer,
        pull_timeout_seconds=17,
    )

    result = await worker.run_once()

    assert result == ActionDispatchPullWorkerResult(1, 0, 1)
    assert subscriber.acknowledged == ()
    assert subscriber.negative == ("ack-1",)
