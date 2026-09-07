from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from eva_ai.notifications.errors import NotificationScopeError
from eva_ai.notifications.repository import NotificationClaim, NotificationRepository
from eva_ai.notifications.types import (
    DeliveryOutcome,
    NotificationDeliveryRequestedMessage,
    NotificationRecord,
    NotificationStatus,
)
from eva_ai.telegram.contracts import TelegramGateway
from eva_ai.telegram.errors import TelegramProviderError


class NotificationDeliveryService:
    def __init__(
        self,
        *,
        notifications: NotificationRepository,
        telegram: TelegramGateway,
        lease_seconds: int,
        max_attempts: int,
        retry_initial_backoff_seconds: float,
        retry_max_backoff_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._notifications = notifications
        self._telegram = telegram
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._retry_initial_backoff_seconds = retry_initial_backoff_seconds
        self._retry_max_backoff_seconds = retry_max_backoff_seconds
        self._clock = clock

    async def process(self, message: NotificationDeliveryRequestedMessage) -> DeliveryOutcome:
        claim = await self._notifications.claim(
            message, now=self._clock(), lease_seconds=self._lease_seconds
        )
        if claim is None:
            record = await self._notifications.get(
                notification_id=message.notification_id,
                user_id=message.user_id,
                workspace_id=message.workspace_id,
            )
            if record is None or record.status in {
                NotificationStatus.SENT,
                NotificationStatus.PERMANENT_FAILURE,
            }:
                return DeliveryOutcome.TERMINAL
            return DeliveryOutcome.DEFERRED
        try:
            subject = await self._notifications.load_delivery_subject(claim)
            sent = await self._telegram.send_message(
                chat_id=subject.chat_id,
                text=subject.notification.message,
            )
            if sent.chat_id != subject.chat_id:
                return await self._terminal(claim, "INVALID_DELIVERY_TARGET")
            await self._notifications.mark_sent(
                claim,
                telegram_account_id=subject.telegram_account_id,
                chat_id=sent.chat_id,
                provider_message_id=sent.message_id,
                sent_at=self._clock(),
            )
            return DeliveryOutcome.SENT
        except NotificationScopeError:
            return await self._terminal(claim, "DELIVERY_TARGET_UNAVAILABLE")
        except TelegramProviderError as error:
            return await self._failure(claim, error.code, retryable=error.retryable)
        except Exception:
            # Provider errors can contain bot tokens or message text, so only stable codes persist.
            return await self._failure(claim, "UNEXPECTED_DELIVERY_FAILURE", retryable=True)

    async def _terminal(self, claim: NotificationClaim, code: str) -> DeliveryOutcome:
        await self._notifications.fail(
            claim,
            retryable=False,
            max_attempts=self._max_attempts,
            next_retry_at=None,
            failure_code=code,
            failure_summary="Telegram delivery cannot continue without corrected configuration.",
        )
        return DeliveryOutcome.TERMINAL

    async def _failure(
        self, claim: NotificationClaim, code: str, *, retryable: bool
    ) -> DeliveryOutcome:
        delay = min(
            self._retry_max_backoff_seconds,
            self._retry_initial_backoff_seconds * (2 ** max(0, claim.attempt_count - 1)),
        )
        record: NotificationRecord = await self._notifications.fail(
            claim,
            retryable=retryable,
            max_attempts=self._max_attempts,
            next_retry_at=self._clock() + timedelta(seconds=delay) if retryable else None,
            failure_code=code,
            failure_summary="Telegram delivery failed and may require retry.",
        )
        return (
            DeliveryOutcome.RETRY
            if record.status is NotificationStatus.RETRYABLE_FAILURE
            else DeliveryOutcome.TERMINAL
        )
