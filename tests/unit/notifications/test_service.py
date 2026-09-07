from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid7

import pytest

from eva_ai.agent.types import NotificationUrgency
from eva_ai.notifications.repository import (
    NotificationClaim,
    NotificationDeliverySubject,
    NotificationRepository,
)
from eva_ai.notifications.service import NotificationDeliveryService
from eva_ai.notifications.types import (
    DeliveryOutcome,
    NotificationChannel,
    NotificationDeliveryRequestedMessage,
    NotificationKind,
    NotificationRecord,
    NotificationStatus,
)
from eva_ai.telegram.contracts import TelegramGateway
from eva_ai.telegram.errors import TelegramProviderError
from eva_ai.telegram.types import TelegramSendResult

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _record(*, status: NotificationStatus = NotificationStatus.SENDING) -> NotificationRecord:
    return NotificationRecord(
        id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        event_id=uuid7(),
        situation_id=uuid7(),
        agent_run_id=None,
        channel=NotificationChannel.TELEGRAM,
        kind=NotificationKind.REACTIVE,
        urgency=NotificationUrgency.MEDIUM,
        message="A scoped reply",
        dedupe_key="turn:test",
        status=status,
        attempt_count=1,
        next_retry_at=None,
        claim_id=uuid7(),
        lease_expires_at=NOW,
        telegram_account_id=None,
        provider_chat_id=None,
        provider_message_id=None,
        failure_code=None,
        failure_summary=None,
        created_at=NOW,
        sent_at=None,
    )


class FakeRepository:
    def __init__(self, record: NotificationRecord) -> None:
        self.record = record
        self.claim_value = NotificationClaim(
            notification_id=record.id,
            claim_id=cast(UUID, record.claim_id),
            user_id=record.user_id,
            workspace_id=record.workspace_id,
            attempt_count=record.attempt_count,
        )
        self.sent: tuple[int, int] | None = None
        self.failed: tuple[bool, str] | None = None

    async def claim(self, *args: object, **kwargs: object) -> NotificationClaim:
        return self.claim_value

    async def load_delivery_subject(self, claim: NotificationClaim) -> NotificationDeliverySubject:
        assert claim is self.claim_value
        return NotificationDeliverySubject(self.record, uuid7(), 12345)

    async def mark_sent(self, claim: NotificationClaim, **kwargs: object) -> NotificationRecord:
        assert claim is self.claim_value
        self.sent = (cast(int, kwargs["chat_id"]), cast(int, kwargs["provider_message_id"]))
        return self.record.model_copy(update={"status": NotificationStatus.SENT})

    async def fail(self, claim: NotificationClaim, **kwargs: object) -> NotificationRecord:
        assert claim is self.claim_value
        retryable = cast(bool, kwargs["retryable"])
        code = cast(str, kwargs["failure_code"])
        self.failed = (retryable, code)
        return self.record.model_copy(
            update={
                "status": (
                    NotificationStatus.RETRYABLE_FAILURE
                    if retryable
                    else NotificationStatus.PERMANENT_FAILURE
                )
            }
        )


class FakeTelegram:
    def __init__(self, failure: TelegramProviderError | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[int, str]] = []

    async def send_message(self, *, chat_id: int, text: str) -> TelegramSendResult:
        self.calls.append((chat_id, text))
        if self.failure is not None:
            raise self.failure
        return TelegramSendResult(message_id=678, chat_id=chat_id)


def _message(record: NotificationRecord) -> NotificationDeliveryRequestedMessage:
    return NotificationDeliveryRequestedMessage(
        outbox_message_id=uuid7(),
        notification_id=record.id,
        event_id=record.event_id,
        user_id=record.user_id,
        workspace_id=record.workspace_id,
    )


def _service(repository: FakeRepository, telegram: FakeTelegram) -> NotificationDeliveryService:
    return NotificationDeliveryService(
        notifications=cast(NotificationRepository, repository),
        telegram=cast(TelegramGateway, telegram),
        lease_seconds=60,
        max_attempts=3,
        retry_initial_backoff_seconds=2,
        retry_max_backoff_seconds=30,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_delivery_persists_provider_message_anchor() -> None:
    record = _record()
    repository = FakeRepository(record)
    telegram = FakeTelegram()

    outcome = await _service(repository, telegram).process(_message(record))

    assert outcome is DeliveryOutcome.SENT
    assert telegram.calls == [(12345, "A scoped reply")]
    assert repository.sent == (12345, 678)


@pytest.mark.asyncio
async def test_retryable_provider_failure_is_sanitized_and_requeued() -> None:
    record = _record()
    repository = FakeRepository(record)
    telegram = FakeTelegram(TelegramProviderError("TELEGRAM_TIMEOUT", retryable=True))

    outcome = await _service(repository, telegram).process(_message(record))

    assert outcome is DeliveryOutcome.RETRY
    assert repository.sent is None
    assert repository.failed == (True, "TELEGRAM_TIMEOUT")
