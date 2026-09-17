import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from eva_ai.actions.types import (
    ActionExecutionRequestedMessage,
    ActionRecord,
    ActionStatus,
    ActionTaskRequest,
    GmailActionCapability,
)
from eva_ai.connectors.gmail.contracts import PullMessage, PullSubscriber

_LOGGER = logging.getLogger(__name__)
_TERMINAL_STATUSES = {ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.UNKNOWN}


class ActionDispatchStore(Protocol):
    async def get_action(self, action_id: UUID) -> ActionRecord | None: ...

    async def record_cloud_task_name(
        self,
        *,
        action_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        cloud_task_name: str,
    ) -> bool: ...


class ActionTaskEnqueuer(Protocol):
    async def enqueue(
        self,
        request: ActionTaskRequest,
        capability: GmailActionCapability,
    ) -> str: ...


class ActionDispatchTransportError(RuntimeError):
    """Content-free action-dispatch transport failure."""


@dataclass(frozen=True, slots=True)
class ActionDispatchPullWorkerResult:
    pulled: int
    acknowledged: int
    negative_acknowledged: int


class ActionDispatchPullWorker:
    def __init__(
        self,
        subscriber: PullSubscriber,
        store: ActionDispatchStore,
        enqueuer: ActionTaskEnqueuer,
        pull_timeout_seconds: int,
    ) -> None:
        self._subscriber = subscriber
        self._store = store
        self._enqueuer = enqueuer
        self._pull_timeout_seconds = pull_timeout_seconds

    async def run_once(self, max_messages: int = 10) -> ActionDispatchPullWorkerResult:
        try:
            messages = await self._subscriber.pull(max_messages, self._pull_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ActionDispatchTransportError("action dispatch pull failed") from None

        acknowledge: list[str] = []
        negative: list[str] = []
        for message in messages:
            (acknowledge if await self._should_ack(message) else negative).append(message.ack_id)

        try:
            if acknowledge:
                await self._subscriber.acknowledge(tuple(acknowledge))
            if negative:
                await self._subscriber.negative_acknowledge(tuple(negative))
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ActionDispatchTransportError("action dispatch acknowledgement failed") from None
        return ActionDispatchPullWorkerResult(
            pulled=len(messages),
            acknowledged=len(acknowledge),
            negative_acknowledged=len(negative),
        )

    async def run_forever(self) -> None:
        primary: BaseException | None = None
        try:
            while True:
                await self.run_once()
        except BaseException as error:
            primary = error
        try:
            await self._subscriber.close()
        except BaseException:
            if primary is None:
                raise ActionDispatchTransportError(
                    "action dispatch subscriber close failed"
                ) from None
        if primary is not None:
            raise primary

    async def _should_ack(self, message: PullMessage) -> bool:
        try:
            envelope = ActionExecutionRequestedMessage.model_validate_json(message.data)
        except ValidationError:
            _log_outcome(message, "poison_acknowledged")
            return True

        try:
            action = await self._store.get_action(envelope.action_id)
            if action is None:
                _log_outcome(message, "missing_acknowledged")
                return True
            # Pub/Sub payload scope is untrusted. It must exactly match the durable Action before
            # the payload is allowed to schedule private execution.
            if action.user_id != envelope.user_id or action.workspace_id != envelope.workspace_id:
                _log_outcome(message, "scope_invalid_acknowledged")
                return True
            if action.status in _TERMINAL_STATUSES:
                _log_outcome(message, "terminal_acknowledged")
                return True
            cloud_task_name = await self._enqueuer.enqueue(
                ActionTaskRequest(
                    action_id=action.id,
                    schema_version=envelope.schema_version,
                ),
                action.capability,
            )
            recorded = await self._store.record_cloud_task_name(
                action_id=action.id,
                user_id=action.user_id,
                workspace_id=action.workspace_id,
                cloud_task_name=cloud_task_name,
            )
            if not recorded:
                raise ActionDispatchTransportError("action task audit update failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            _log_outcome(message, "failed_negative_acknowledged")
            return False
        _log_outcome(message, "enqueued_acknowledged")
        return True


def _log_outcome(message: PullMessage, outcome: str) -> None:
    _LOGGER.info(
        "action dispatch message processed",
        extra={"pubsub_message_id": message.message_id, "outcome": outcome},
    )
