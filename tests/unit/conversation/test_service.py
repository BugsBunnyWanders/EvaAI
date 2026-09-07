import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import cast
from uuid import uuid7

from eva_ai.conversation.repository import ConversationRepository, ConversationTurnClaim
from eva_ai.conversation.service import ConversationService
from eva_ai.conversation.types import ConversationOutcome
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


async def test_exhausted_retry_completes_with_user_visible_fallback() -> None:
    class Conversations:
        def __init__(self) -> None:
            self.completed: dict[str, object] | None = None

        async def complete(self, claim: ConversationTurnClaim, **values: object) -> None:
            self.completed = values

        async def fail(self, claim: ConversationTurnClaim, **values: object) -> None:
            raise AssertionError("exhausted retries must not silently fail the turn")

    conversations = Conversations()
    service = object.__new__(ConversationService)
    service._conversations = cast(ConversationRepository, conversations)
    service._max_attempts = 1
    service._agent_version = "conversation-v2"
    service._clock = lambda: datetime(2030, 1, 1, tzinfo=UTC)
    claim = ConversationTurnClaim(
        turn_id=uuid7(),
        conversation_id=uuid7(),
        claim_id=uuid7(),
        event_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        attempt_count=1,
    )

    outcome = await service._retry(claim, "MODEL_OUTPUT_INVALID")

    assert outcome is ConversationOutcome.SUCCEEDED
    assert conversations.completed is not None
    assert "try once more" in str(conversations.completed["response_text"])
    assert conversations.completed["provider_response_id"] is None
