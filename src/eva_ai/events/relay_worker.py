import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from eva_ai.events.outbox import PublishBatchResult

_LOGGER = logging.getLogger(__name__)


class OutboxBatchRelay(Protocol):
    async def publish_batch(self, limit: int) -> PublishBatchResult: ...


class OutboxRelayWorker:
    def __init__(
        self,
        relay: OutboxBatchRelay,
        batch_limit: int,
        poll_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._relay = relay
        self._batch_limit = batch_limit
        self._poll_seconds = poll_seconds
        self._sleep = sleep

    async def run_once(self) -> PublishBatchResult:
        return await self._relay.publish_batch(self._batch_limit)

    async def run_forever(self) -> None:
        while True:
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Provider/database messages may contain credentials and are never logged.
                _LOGGER.warning("event relay batch failed", extra={"outcome": "failed"})
                await self._sleep(self._poll_seconds)
                continue
            if (
                result.claimed == self._batch_limit
                and result.published == self._batch_limit
                and result.failed == 0
            ):
                continue
            await self._sleep(self._poll_seconds)
