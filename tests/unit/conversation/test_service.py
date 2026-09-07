import asyncio
from contextlib import suppress
from typing import cast

from eva_ai.conversation.service import ConversationService
from eva_ai.telegram.contracts import TelegramGateway


class Telegram:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.actions: list[tuple[int, str]] = []
        self.called = asyncio.Event()

    async def send_chat_action(self, *, chat_id: int, action: str) -> None:
        self.actions.append((chat_id, action))
        self.called.set()
        if self.fail:
            raise RuntimeError("provider unavailable")


async def test_typing_refresh_starts_immediately_and_repeats() -> None:
    telegram = Telegram()
    service = object.__new__(ConversationService)
    service._telegram = cast(TelegramGateway, telegram)
    service._typing_refresh_seconds = 0.001

    task = asyncio.create_task(service._refresh_typing(42))
    await asyncio.wait_for(telegram.called.wait(), timeout=1)
    await asyncio.sleep(0.003)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert len(telegram.actions) >= 2
    assert set(telegram.actions) == {(42, "typing")}


async def test_typing_refresh_ignores_provider_failure() -> None:
    telegram = Telegram(fail=True)
    service = object.__new__(ConversationService)
    service._telegram = cast(TelegramGateway, telegram)
    service._typing_refresh_seconds = 60

    task = asyncio.create_task(service._refresh_typing(42))
    await asyncio.wait_for(telegram.called.wait(), timeout=1)
    assert task.done() is False
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
