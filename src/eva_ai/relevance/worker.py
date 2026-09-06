import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from eva_ai.connectors.gmail.contracts import PullMessage, PullSubscriber
from eva_ai.events.processor import EventHandler, ProcessOutcome, ProcessResult
from eva_ai.events.types import EventAvailableMessage
from eva_ai.relevance.errors import EvaluationReviewRequired

_LOGGER = logging.getLogger(__name__)
_ACK_OUTCOMES = {ProcessOutcome.HANDLED, ProcessOutcome.ALREADY_HANDLED}


class EventDispatcher(Protocol):
    async def process(
        self, message: EventAvailableMessage, handler: EventHandler
    ) -> ProcessResult: ...


class RelevanceWorkerTransportError(RuntimeError):
    """Content-free Pub/Sub transport failure."""


@dataclass(frozen=True, slots=True)
class RelevancePullBatchResult:
    pulled: int
    acknowledged: int
    negative_acknowledged: int


class RelevancePullWorker:
    def __init__(
        self,
        subscriber: PullSubscriber,
        processor: EventDispatcher,
        handler: EventHandler,
        pull_timeout_seconds: int,
    ) -> None:
        self._subscriber = subscriber
        self._processor = processor
        self._handler = handler
        self._pull_timeout_seconds = pull_timeout_seconds

    async def run_once(self, max_messages: int = 10) -> RelevancePullBatchResult:
        try:
            messages = await self._subscriber.pull(max_messages, self._pull_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RelevanceWorkerTransportError("relevance pull failed") from None

        acknowledge: list[str] = []
        negative_acknowledge: list[str] = []
        for message in messages:
            target = (
                acknowledge if await self._should_acknowledge(message) else negative_acknowledge
            )
            target.append(message.ack_id)

        try:
            if acknowledge:
                await self._subscriber.acknowledge(tuple(acknowledge))
            if negative_acknowledge:
                await self._subscriber.negative_acknowledge(tuple(negative_acknowledge))
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RelevanceWorkerTransportError("relevance acknowledgement failed") from None
        return RelevancePullBatchResult(
            pulled=len(messages),
            acknowledged=len(acknowledge),
            negative_acknowledged=len(negative_acknowledge),
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
                raise RelevanceWorkerTransportError("relevance subscriber close failed") from None
        if primary is not None:
            raise primary

    async def _should_acknowledge(self, message: PullMessage) -> bool:
        try:
            envelope = EventAvailableMessage.model_validate_json(message.data)
            result = await self._processor.process(envelope, self._handler)
        except ValidationError:
            _log_outcome(message, "poison_acknowledged")
            return True
        except EvaluationReviewRequired:
            _log_outcome(message, "review_required_acknowledged")
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            _log_outcome(message, "failed_negative_acknowledged")
            return False
        acknowledge = result.outcome in _ACK_OUTCOMES
        _log_outcome(message, "acknowledged" if acknowledge else "busy_negative_acknowledged")
        return acknowledge


def _log_outcome(message: PullMessage, outcome: str) -> None:
    _LOGGER.info(
        "relevance message processed",
        extra={"pubsub_message_id": message.message_id, "outcome": outcome},
    )
