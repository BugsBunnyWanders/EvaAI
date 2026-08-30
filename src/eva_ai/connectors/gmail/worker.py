import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from eva_ai.connectors.gmail.contracts import (
    HistoryCursorExpired,
    InvalidNotification,
    PullMessage,
    PullSubscriber,
)
from eva_ai.connectors.gmail.maintenance import GmailMaintenanceService
from eva_ai.connectors.gmail.notification import decode_notification
from eva_ai.connectors.gmail.sync import (
    GmailSyncError,
    GmailSyncService,
    SyncResult,
    SyncStatus,
)
from eva_ai.connectors.repository import AmbiguousConnectorIdentity
from eva_ai.integrations.gcp.secret_manager import SecretManagerProviderError
from eva_ai.integrations.gmail.api import GmailProviderError

logger = logging.getLogger(__name__)

_ACK_STATUSES = {
    SyncStatus.SYNCED,
    SyncStatus.ALREADY_COVERED,
    SyncStatus.UNKNOWN_ACCOUNT,
    SyncStatus.REAUTHORIZATION_REQUIRED,
}
_NACK_STATUSES = {SyncStatus.CONNECTING, SyncStatus.BUSY}


@dataclass(frozen=True, slots=True)
class PullBatchResult:
    pulled: int
    acknowledged: int
    negative_acknowledged: int


class GmailWorkerTransportError(RuntimeError):
    """Transport failure with provider-controlled content removed."""


class GmailWorkerMaintenanceError(RuntimeError):
    """Maintenance failure with external content removed."""


class GmailPullWorker:
    def __init__(
        self,
        subscriber: PullSubscriber,
        sync_service: GmailSyncService,
        maintenance: GmailMaintenanceService,
        clock: Callable[[], datetime],
        pull_timeout_seconds: int,
    ) -> None:
        self._subscriber = subscriber
        self._sync_service = sync_service
        self._maintenance = maintenance
        self._clock = clock
        self._pull_timeout_seconds = pull_timeout_seconds

    async def run_once(self, max_messages: int = 10) -> PullBatchResult:
        messages = await self._pull(max_messages)
        acknowledge: list[str] = []
        negative_acknowledge: list[str] = []

        for message in messages:
            if await self._should_acknowledge(message):
                acknowledge.append(message.ack_id)
            else:
                negative_acknowledge.append(message.ack_id)

        # Decisions are grouped only after every message has been classified independently.
        transport_failed = False
        if acknowledge:
            try:
                await self._subscriber.acknowledge(tuple(acknowledge))
            except Exception:
                transport_failed = True
        if negative_acknowledge:
            try:
                await self._subscriber.negative_acknowledge(tuple(negative_acknowledge))
            except Exception:
                transport_failed = True

        maintenance_failed = False
        try:
            summary = await self._maintenance.run_due(self._clock())
        except Exception:
            maintenance_failed = True
            summary = None

        if summary is not None and summary.failed:
            _log_operational_failure(
                "Gmail maintenance completed with failures",
                operation="maintenance",
                outcome="failed",
                error_category="maintenance_failed",
            )
        if maintenance_failed:
            _log_operational_failure(
                "Gmail maintenance failed",
                operation="maintenance",
                outcome="failed",
                error_category="maintenance_failed",
            )
        if transport_failed:
            _log_operational_failure(
                "Gmail acknowledgement transport failed",
                operation="acknowledgement",
                outcome="failed",
                error_category="transport_failed",
            )
            raise GmailWorkerTransportError("Gmail acknowledgement transport failed")
        if maintenance_failed:
            raise GmailWorkerMaintenanceError("Gmail maintenance failed")

        return PullBatchResult(
            pulled=len(messages),
            acknowledged=len(acknowledge),
            negative_acknowledged=len(negative_acknowledge),
        )

    async def run_forever(self) -> None:
        primary_failure: BaseException | None = None
        close_cancellation: asyncio.CancelledError | None = None
        close_failed = False
        try:
            while True:
                await self.run_once()
        except BaseException as error:
            primary_failure = error
        finally:
            # Preserve the triggering failure while awaiting subscriber shutdown exactly once.
            try:
                await self._subscriber.close()
            except asyncio.CancelledError as error:
                close_cancellation = error
            except Exception:
                close_failed = True

        if primary_failure is not None:
            raise primary_failure
        if close_cancellation is not None:
            raise close_cancellation
        if close_failed:
            _log_operational_failure(
                "Gmail subscriber close failed",
                operation="subscriber_close",
                outcome="failed",
                error_category="transport_failed",
            )
            raise GmailWorkerTransportError("Gmail subscriber close failed")

    async def _pull(self, max_messages: int) -> tuple[PullMessage, ...]:
        failed = False
        try:
            return await self._subscriber.pull(max_messages, self._pull_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            failed = True

        if failed:
            _log_operational_failure(
                "Gmail pull failed",
                operation="pull",
                outcome="failed",
                error_category="transport_failed",
            )
            raise GmailWorkerTransportError("Gmail pull failed")
        raise AssertionError("subscriber pull produced no result")

    async def _should_acknowledge(self, message: PullMessage) -> bool:
        try:
            notification = decode_notification(message.data)
        except InvalidNotification:
            _log_message_outcome(
                "Gmail notification rejected",
                message,
                connector_id=None,
                operation="notification_decode",
                outcome="acknowledged",
                error_category="malformed_notification",
            )
            return True
        except Exception:
            _log_message_outcome(
                "Gmail notification processing failed",
                message,
                connector_id=None,
                operation="notification_decode",
                outcome="negative_acknowledged",
                error_category="internal_failure",
            )
            return False

        result: SyncResult | None = None
        error_category: str | None = None
        try:
            result = await self._sync_service.handle(notification)
        except asyncio.CancelledError:
            raise
        except GmailProviderError:
            error_category = "gmail_provider_transient"
        except SecretManagerProviderError:
            error_category = "credential_provider_transient"
        except (
            AmbiguousConnectorIdentity,
            GmailSyncError,
            HistoryCursorExpired,
            ValueError,
        ):
            error_category = "synchronization_failed"
        except Exception:
            error_category = "internal_failure"

        if error_category is not None:
            _log_message_outcome(
                "Gmail notification processing failed",
                message,
                connector_id=None,
                operation="notification_sync",
                outcome="negative_acknowledged",
                error_category=error_category,
            )
            return False
        assert result is not None
        if result.status in _ACK_STATUSES:
            if result.status is SyncStatus.UNKNOWN_ACCOUNT:
                _log_message_outcome(
                    "Gmail notification account unavailable",
                    message,
                    connector_id=None,
                    operation="notification_sync",
                    outcome="acknowledged",
                    error_category="unknown_account",
                )
            return True
        if result.status in _NACK_STATUSES:
            _log_message_outcome(
                "Gmail notification deferred",
                message,
                connector_id=result.connector_id,
                operation="notification_sync",
                outcome="negative_acknowledged",
                error_category=(
                    "connector_connecting"
                    if result.status is SyncStatus.CONNECTING
                    else "synchronization_busy"
                ),
            )
            return False
        _log_message_outcome(
            "Gmail notification processing failed",
            message,
            connector_id=result.connector_id,
            operation="notification_sync",
            outcome="negative_acknowledged",
            error_category="internal_failure",
        )
        return False


def _log_message_outcome(
    message_text: str,
    message: PullMessage,
    *,
    connector_id: UUID | None,
    operation: str,
    outcome: str,
    error_category: str,
) -> None:
    context: dict[str, object] = {
        "pubsub_message_id": message.message_id,
        "operation": operation,
        "outcome": outcome,
        "error_category": error_category,
    }
    if connector_id is not None:
        context["connector_id"] = connector_id
    logger.warning(message_text, extra=context)


def _log_operational_failure(
    message_text: str,
    *,
    operation: str,
    outcome: str,
    error_category: str,
) -> None:
    logger.warning(
        message_text,
        extra={
            "operation": operation,
            "outcome": outcome,
            "error_category": error_category,
        },
    )
