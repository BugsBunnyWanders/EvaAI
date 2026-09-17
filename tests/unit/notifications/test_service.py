import hashlib
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid7

import pytest

from eva_ai.agent.types import NotificationUrgency
from eva_ai.notifications.repository import (
    ApprovalCardSubject,
    NotificationClaim,
    NotificationDeliverySubject,
    NotificationRepository,
)
from eva_ai.notifications.service import NotificationDeliveryService, build_approval_card
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
from eva_ai.telegram.types import (
    TelegramInlineKeyboardButton,
    TelegramInlineKeyboardMarkup,
    TelegramSendResult,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _record(
    *,
    status: NotificationStatus = NotificationStatus.SENDING,
    reply_markup: TelegramInlineKeyboardMarkup | None = None,
    action_approval_id: UUID | None = None,
) -> NotificationRecord:
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
        reply_markup=reply_markup,
        action_approval_id=action_approval_id,
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
        self.bound_digest: str | None = None
        self.approval_card: ApprovalCardSubject | None = None

    async def claim(self, *args: object, **kwargs: object) -> NotificationClaim:
        return self.claim_value

    async def load_delivery_subject(self, claim: NotificationClaim) -> NotificationDeliverySubject:
        assert claim is self.claim_value
        return NotificationDeliverySubject(self.record, uuid7(), 12345)

    async def mark_sent(self, claim: NotificationClaim, **kwargs: object) -> NotificationRecord:
        assert claim is self.claim_value
        self.sent = (cast(int, kwargs["chat_id"]), cast(int, kwargs["provider_message_id"]))
        return self.record.model_copy(update={"status": NotificationStatus.SENT})

    async def prepare_approval_card(
        self,
        claim: NotificationClaim,
        *,
        callback_token_digest: str,
        now: datetime,
    ) -> ApprovalCardSubject:
        assert claim is self.claim_value
        assert now == NOW
        assert self.approval_card is not None
        self.bound_digest = callback_token_digest
        return self.approval_card

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
        self.calls: list[tuple[int, str, TelegramInlineKeyboardMarkup | None, bool]] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: TelegramInlineKeyboardMarkup | None = None,
        preserve_text: bool = False,
    ) -> TelegramSendResult:
        self.calls.append((chat_id, text, reply_markup, preserve_text))
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
    assert telegram.calls == [(12345, "A scoped reply", None, False)]
    assert repository.sent == (12345, 678)


@pytest.mark.asyncio
async def test_delivery_passes_inline_keyboard_to_telegram() -> None:
    keyboard = TelegramInlineKeyboardMarkup(
        inline_keyboard=(
            (TelegramInlineKeyboardButton(text="Send", callback_data="send:opaque-token"),),
        )
    )
    record = _record(reply_markup=keyboard)
    repository = FakeRepository(record)
    telegram = FakeTelegram()

    outcome = await _service(repository, telegram).process(_message(record))

    assert outcome is DeliveryOutcome.SENT
    assert telegram.calls == [(12345, "A scoped reply", keyboard, False)]


@pytest.mark.asyncio
async def test_approval_delivery_generates_ephemeral_token_and_keyboard_only_on_final_segment() -> (
    None
):
    approval_id = uuid7()
    record = _record(action_approval_id=approval_id)
    repository = FakeRepository(record)
    body = "A complete line of the draft.\n" * 350
    repository.approval_card = ApprovalCardSubject(
        approval_id=approval_id,
        mode="NEW",
        to=("to@example.com",),
        cc=("cc@example.com",),
        bcc=("bcc@example.com",),
        subject="Exact subject",
        text_body=body.rstrip(),
        expires_at=NOW,
    )
    telegram = FakeTelegram()

    outcome = await _service(repository, telegram).process(_message(record))

    assert outcome is DeliveryOutcome.SENT
    assert len(telegram.calls) > 1
    assert all(markup is None for _, _, markup, _ in telegram.calls[:-1])
    assert all(preserve for _, _, _, preserve in telegram.calls)
    final_markup = telegram.calls[-1][2]
    assert final_markup is not None
    callbacks = [button.callback_data for row in final_markup.inline_keyboard for button in row]
    assert [value.split(":", maxsplit=1)[0] for value in callbacks] == [
        "send",
        "change",
        "discard",
    ]
    tokens = {value.split(":", maxsplit=1)[1] for value in callbacks}
    assert len(tokens) == 1
    raw_token = tokens.pop()
    assert repository.bound_digest == hashlib.sha256(raw_token.encode()).hexdigest()
    assert raw_token != repository.bound_digest
    rendered = "".join(text for _, text, _, _ in telegram.calls)
    assert body.rstrip() in rendered
    assert "Mode: New email" in rendered
    assert "To: to@example.com" in rendered
    assert "Cc: cc@example.com" in rendered
    assert "Bcc: bcc@example.com" in rendered
    assert "Subject: Exact subject" in rendered
    assert "Expires:" in rendered


def test_approval_card_splits_without_truncating_complete_body() -> None:
    body = "line with emoji 🚀\n" * 600
    card = build_approval_card(
        ApprovalCardSubject(
            approval_id=uuid7(),
            mode="REPLY",
            to=("to@example.com",),
            cc=(),
            bcc=(),
            subject="Re: Exact subject",
            text_body=body.rstrip(),
            expires_at=NOW,
        ),
        callback_token="opaque-token",
    )

    assert len(card.segments) > 1
    assert all(len(segment.encode("utf-16-le")) // 2 <= 4000 for segment in card.segments)
    assert body.rstrip() in "".join(card.segments)
    callbacks = [
        button.callback_data for row in card.reply_markup.inline_keyboard for button in row
    ]
    assert callbacks == [
        "send:opaque-token",
        "change:opaque-token",
        "discard:opaque-token",
    ]


@pytest.mark.asyncio
async def test_retryable_provider_failure_is_sanitized_and_requeued() -> None:
    record = _record()
    repository = FakeRepository(record)
    telegram = FakeTelegram(TelegramProviderError("TELEGRAM_TIMEOUT", retryable=True))

    outcome = await _service(repository, telegram).process(_message(record))

    assert outcome is DeliveryOutcome.RETRY
    assert repository.sent is None
    assert repository.failed == (True, "TELEGRAM_TIMEOUT")
