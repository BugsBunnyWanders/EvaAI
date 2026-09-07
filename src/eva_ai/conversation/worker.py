import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from eva_ai.connectors.gmail.contracts import PullMessage, PullSubscriber
from eva_ai.conversation.types import ConversationOutcome
from eva_ai.telegram.types import TelegramTurnRequestedMessage

_LOGGER = logging.getLogger(__name__)
_ACK_OUTCOMES = {ConversationOutcome.SUCCEEDED, ConversationOutcome.TERMINAL}


class ConversationMessageHandler(Protocol):
    async def process(self, message: TelegramTurnRequestedMessage) -> ConversationOutcome: ...


class ConversationWorkerTransportError(RuntimeError):
    """Content-free Pub/Sub transport failure."""


@dataclass(frozen=True, slots=True)
class ConversationPullBatchResult:
    pulled: int
    acknowledged: int
    negative_acknowledged: int


class ConversationPullWorker:
    def __init__(
        self,
        subscriber: PullSubscriber,
        handler: ConversationMessageHandler,
        pull_timeout_seconds: int,
    ) -> None:
        self._subscriber = subscriber
        self._handler = handler
        self._pull_timeout_seconds = pull_timeout_seconds

    async def run_once(self, max_messages: int = 10) -> ConversationPullBatchResult:
        try:
            messages = await self._subscriber.pull(max_messages, self._pull_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ConversationWorkerTransportError("conversation pull failed") from None
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
            raise ConversationWorkerTransportError("conversation acknowledgement failed") from None
        return ConversationPullBatchResult(len(messages), len(acknowledge), len(negative))

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
                raise ConversationWorkerTransportError(
                    "conversation subscriber close failed"
                ) from None
        if primary is not None:
            raise primary

    async def _should_ack(self, message: PullMessage) -> bool:
        try:
            envelope = TelegramTurnRequestedMessage.model_validate_json(message.data)
            outcome = await self._handler.process(envelope)
        except ValidationError:
            _log(message, "poison_acknowledged")
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            _log(message, "failed_negative_acknowledged")
            return False
        acknowledge = outcome in _ACK_OUTCOMES
        _log(message, "acknowledged" if acknowledge else "deferred_negative_acknowledged")
        return acknowledge


def _log(message: PullMessage, outcome: str) -> None:
    _LOGGER.info(
        "conversation message processed",
        extra={"pubsub_message_id": message.message_id, "outcome": outcome},
    )
