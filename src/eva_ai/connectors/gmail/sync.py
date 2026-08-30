from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from eva_ai.connectors.gmail.contracts import (
    AuthorizationRevoked,
    CredentialStore,
    GmailClientFactory,
    GmailNotification,
)
from eva_ai.connectors.gmail.normalizer import normalize_message
from eva_ai.connectors.repository import ConnectorRepository
from eva_ai.connectors.types import ConnectorRecord, ConnectorStatus, GmailSyncRecord, SyncClaim
from eva_ai.events.service import EventService

_SAFETY_SYNC_INTERVAL = timedelta(minutes=60)


class SyncStatus(StrEnum):
    SYNCED = "SYNCED"
    ALREADY_COVERED = "ALREADY_COVERED"
    BUSY = "BUSY"
    CONNECTING = "CONNECTING"
    REAUTHORIZATION_REQUIRED = "REAUTHORIZATION_REQUIRED"
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"


@dataclass(frozen=True, slots=True)
class SyncResult:
    status: SyncStatus
    connector_id: UUID | None
    events_created: int
    final_history_id: str | None


class GmailSyncError(RuntimeError):
    """Retryable synchronization failure with external content removed."""


class GmailSyncService:
    def __init__(
        self,
        repository: ConnectorRepository,
        credential_store: CredentialStore,
        client_factory: GmailClientFactory,
        event_service: EventService,
        clock: Callable[[], datetime],
        lease_seconds: int,
    ) -> None:
        self._repository = repository
        self._credential_store = credential_store
        self._client_factory = client_factory
        self._event_service = event_service
        self._clock = clock
        self._lease_seconds = lease_seconds

    async def handle(self, notification: GmailNotification) -> SyncResult:
        connector = await self._repository.find_by_identity(notification.email_address)
        if connector is None or connector.status == ConnectorStatus.DISABLED:
            return SyncResult(SyncStatus.UNKNOWN_ACCOUNT, None, 0, None)

        sync = await self._repository.get_sync_state(connector.id)
        inactive = self._inactive_result(connector, sync)
        if inactive is not None:
            return inactive

        # The notification is only a wake hint; the persisted cursor decides the range.
        if sync is not None and _cursor_covers(sync.history_id, notification.history_id):
            return SyncResult(
                SyncStatus.ALREADY_COVERED,
                connector.id,
                0,
                sync.history_id,
            )
        return await self.sync_connector(connector.id)

    async def sync_connector(self, connector_id: UUID) -> SyncResult:
        connector = await self._repository.get(connector_id)
        if connector is None or connector.status == ConnectorStatus.DISABLED:
            return SyncResult(SyncStatus.UNKNOWN_ACCOUNT, None, 0, None)

        sync = await self._repository.get_sync_state(connector_id)
        inactive = self._inactive_result(connector, sync)
        if inactive is not None:
            return inactive

        claim = await self._repository.claim_sync(
            connector_id,
            self._clock(),
            self._lease_seconds,
        )
        if claim is None:
            return SyncResult(
                SyncStatus.BUSY,
                connector_id,
                0,
                sync.history_id if sync is not None else None,
            )
        return await self._synchronize_claim(claim)

    async def _synchronize_claim(self, claim: SyncClaim) -> SyncResult:
        revoked: AuthorizationRevoked | None = None
        try:
            secret_reference, start_history_id, connected_at = _claim_inputs(claim)
            authorized_user_json = await self._credential_store.get(secret_reference)
            gmail = await self._client_factory.create(authorized_user_json)

            # Claim acquisition is already committed; no SQL transaction spans provider I/O.
            message_ids: dict[str, None] = {}
            page_token: str | None = None
            final_history_id = start_history_id
            while True:
                page = await gmail.list_history(start_history_id, page_token)
                final_history_id = page.history_id
                for message_id in page.message_ids:
                    message_ids.setdefault(message_id, None)
                page_token = page.next_page_token
                if page_token is None:
                    break

            events_created = 0
            for message_id in message_ids:
                raw = await gmail.get_message(message_id)
                if not _has_inbox_label(raw):
                    continue
                event = normalize_message(raw, claim.connector, final_history_id)
                if event.occurred_at < connected_at:
                    continue
                # Every Event commits independently before the cursor completion transaction.
                result = await self._event_service.ingest(event)
                events_created += int(result.created)

            completed_at = self._clock()
            completed = await self._repository.complete_sync(
                claim,
                final_history_id,
                completed_at,
                completed_at + _SAFETY_SYNC_INTERVAL,
            )
            if not completed:
                raise GmailSyncError("Gmail synchronization claim is no longer current")
            return SyncResult(
                SyncStatus.SYNCED,
                claim.connector.id,
                events_created,
                final_history_id,
            )
        except AuthorizationRevoked as error:
            revoked = error
        except Exception:
            await self._release_safely(claim)
            raise

        if await self._mark_reauthorization(claim.connector.id, revoked):
            return SyncResult(
                SyncStatus.REAUTHORIZATION_REQUIRED,
                claim.connector.id,
                0,
                claim.sync.history_id,
            )
        await self._release_safely(claim)
        raise GmailSyncError("Gmail synchronization state transition failed")

    async def _release_safely(self, claim: SyncClaim) -> None:
        try:
            await self._repository.release_sync(claim)
        except Exception:
            # The bounded lease remains reclaimable; a release error must not mask the cause.
            pass

    async def _mark_reauthorization(
        self,
        connector_id: UUID,
        error: AuthorizationRevoked | None,
    ) -> bool:
        if error is None:
            return False
        try:
            await self._repository.mark_reauthorization_required(connector_id, error)
        except Exception:
            return False
        return True

    @staticmethod
    def _inactive_result(
        connector: ConnectorRecord,
        sync: GmailSyncRecord | None,
    ) -> SyncResult | None:
        history_id = sync.history_id if sync is not None else None
        if connector.status in {ConnectorStatus.CONNECTING, ConnectorStatus.ERROR}:
            return SyncResult(SyncStatus.CONNECTING, connector.id, 0, history_id)
        if connector.status == ConnectorStatus.REAUTHORIZATION_REQUIRED:
            return SyncResult(
                SyncStatus.REAUTHORIZATION_REQUIRED,
                connector.id,
                0,
                history_id,
            )
        return None


def _cursor_covers(persisted: str | None, notification: str) -> bool:
    return (
        persisted is not None
        and persisted.isascii()
        and persisted.isdecimal()
        and notification.isascii()
        and notification.isdecimal()
        and int(persisted) >= int(notification)
    )


def _claim_inputs(claim: SyncClaim) -> tuple[str, str, datetime]:
    secret_reference = claim.connector.secret_reference
    history_id = claim.sync.history_id
    connected_at = claim.connector.connected_at
    if secret_reference is None or history_id is None or connected_at is None:
        raise GmailSyncError("Gmail connector is not ready for synchronization")
    return secret_reference, history_id, connected_at


def _has_inbox_label(raw: Mapping[str, object]) -> bool:
    labels = raw.get("labelIds")
    return isinstance(labels, list) and "INBOX" in labels
