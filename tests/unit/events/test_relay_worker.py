import asyncio

import pytest

from eva_ai.events.outbox import PublishBatchResult
from eva_ai.events.relay_worker import OutboxRelayWorker


class ScriptedRelay:
    def __init__(self, results: list[PublishBatchResult]) -> None:
        self.results = results
        self.limits: list[int] = []

    async def publish_batch(self, limit: int) -> PublishBatchResult:
        self.limits.append(limit)
        if not self.results:
            raise asyncio.CancelledError
        return self.results.pop(0)


async def test_relay_drains_full_batches_then_waits_after_partial() -> None:
    relay = ScriptedRelay([PublishBatchResult(10, 10, 0), PublishBatchResult(2, 2, 0)])
    delays: list[float] = []

    async def stop_after_sleep(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await OutboxRelayWorker(relay, 10, 1.5, stop_after_sleep).run_forever()

    assert relay.limits == [10, 10]
    assert delays == [1.5]
