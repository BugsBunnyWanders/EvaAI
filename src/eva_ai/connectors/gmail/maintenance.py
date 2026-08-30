import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from eva_ai.connectors.gmail.contracts import (
    AuthorizationRevoked,
    CredentialStore,
    GmailClientFactory,
)
from eva_ai.connectors.gmail.sync import GmailSyncService, SyncStatus
from eva_ai.connectors.repository import ConnectorRepository
from eva_ai.connectors.types import GmailSyncRecord, SyncClaim

_WATCH_RENEWAL_INTERVAL = timedelta(hours=24)
_SAFETY_SYNC_INTERVAL = timedelta(minutes=60)


@dataclass(frozen=True, slots=True)
class MaintenanceSummary:
    renewed: int
    safety_synced: int
    failed: int


class GmailMaintenanceService:
    def __init__(
        self,
        repository: ConnectorRepository,
        credential_store: CredentialStore,
        client_factory: GmailClientFactory,
        sync_service: GmailSyncService,
        topic_name: str,
        lease_seconds: int,
    ) -> None:
        self._repository = repository
        self._credential_store = credential_store
        self._client_factory = client_factory
        self._sync_service = sync_service
        self._topic_name = topic_name
        self._lease_seconds = lease_seconds

    async def run_due(self, now: datetime) -> MaintenanceSummary:
        renewed = 0
        safety_synced = 0
        failed = 0
        try:
            connector_ids = await self._repository.due_for_maintenance(now)
        except asyncio.CancelledError:
            raise
        except Exception:
            return MaintenanceSummary(renewed=0, safety_synced=0, failed=1)

        for connector_id in connector_ids:
            state = await self._load_state(connector_id)
            if state is None:
                failed += 1
                continue

            if _is_due(state.next_watch_renewal_at, now):
                outcome = await self._renew(connector_id, now)
                renewed += int(outcome is True)
                failed += int(outcome is False)

            # Renewal owns and clears its lease before safety obtains a new sync claim.
            state = await self._load_state(connector_id)
            if state is None:
                failed += 1
                continue
            if _is_safety_due(state, now):
                try:
                    result = await self._sync_service.sync_connector(connector_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failed += 1
                else:
                    if result.status == SyncStatus.SYNCED:
                        safety_synced += 1
                    else:
                        failed += 1

        return MaintenanceSummary(
            renewed=renewed,
            safety_synced=safety_synced,
            failed=failed,
        )

    async def _load_state(self, connector_id: UUID) -> GmailSyncRecord | None:
        try:
            return await self._repository.get_sync_state(connector_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _renew(self, connector_id: UUID, now: datetime) -> bool | None:
        claim: SyncClaim | None = None
        revoked: AuthorizationRevoked | None = None
        try:
            claim = await self._repository.claim_sync(
                connector_id,
                now,
                self._lease_seconds,
            )
            if claim is None:
                return False
            if not _is_due(claim.sync.next_watch_renewal_at, now):
                await self._release_safely(claim)
                return None

            secret_reference = claim.connector.secret_reference
            if secret_reference is None:
                await self._release_safely(claim)
                return False
            authorized_user_json = await self._credential_store.get(secret_reference)
            gmail = await self._client_factory.create(authorized_user_json)
            watch = await gmail.watch(self._topic_name)
            renewed = await self._repository.record_watch_renewal(
                claim,
                watch.expiration,
                min(
                    now + _WATCH_RENEWAL_INTERVAL,
                    watch.expiration - _WATCH_RENEWAL_INTERVAL,
                ),
            )
            if not renewed:
                await self._release_safely(claim)
                return False
            return True
        except asyncio.CancelledError:
            if claim is not None:
                await self._release_after_cancellation(claim)
            raise
        except AuthorizationRevoked as error:
            revoked = error
        except Exception:
            if claim is not None:
                await self._release_safely(claim)
            return False

        if claim is None or revoked is None:
            return False
        transition_cancelled: asyncio.CancelledError | None = None
        try:
            transitioned = await self._mark_reauthorization(claim, revoked)
        except asyncio.CancelledError as error:
            transition_cancelled = error
            transitioned = False
        if transition_cancelled is not None:
            await self._release_after_cancellation(claim)
            raise transition_cancelled
        if not transitioned:
            await self._release_safely(claim)
        return False

    async def _mark_reauthorization(
        self,
        claim: SyncClaim,
        error: AuthorizationRevoked,
    ) -> bool:
        try:
            await self._repository.mark_reauthorization_required(claim.connector.id, error)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return True

    async def _release_safely(self, claim: SyncClaim) -> None:
        try:
            await self._repository.release_sync(claim)
        except Exception:
            pass

    async def _release_after_cancellation(self, claim: SyncClaim) -> None:
        try:
            await self._repository.release_sync(claim)
        except BaseException:
            pass


def _is_due(due_at: datetime | None, now: datetime) -> bool:
    return due_at is not None and due_at <= now


def _is_safety_due(state: GmailSyncRecord, now: datetime) -> bool:
    notification_at = state.last_notification_at
    silence_elapsed = notification_at is None or notification_at + _SAFETY_SYNC_INTERVAL <= now
    return _is_due(state.next_safety_sync_at, now) and silence_elapsed
