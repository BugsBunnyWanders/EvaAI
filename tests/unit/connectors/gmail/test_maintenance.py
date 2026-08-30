import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid7

import pytest

from eva_ai.connectors.gmail.contracts import (
    AuthorizationRevoked,
    CredentialStore,
    GmailClient,
    GmailClientFactory,
    HistoryPage,
    MessageListPage,
    WatchResult,
)
from eva_ai.connectors.gmail.maintenance import GmailMaintenanceService, MaintenanceSummary
from eva_ai.connectors.gmail.sync import GmailSyncService, SyncResult, SyncStatus
from eva_ai.connectors.repository import ConnectorRepository
from eva_ai.connectors.types import ConnectorRecord, ConnectorStatus, GmailSyncRecord, SyncClaim

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)
USER_ID = UUID("0191cafe-7b00-7000-8000-000000000001")
WORKSPACE_ID = UUID("0191cafe-7b00-7000-8000-000000000002")
CONNECTOR_ID = UUID("0191cafe-7b00-7000-8000-000000000003")
OTHER_CONNECTOR_ID = UUID("0191cafe-7b00-7000-8000-000000000004")
AUTHORIZED_USER_JSON = '{"type":"authorized_user","refresh_token":"synthetic"}'
TOPIC_NAME = "projects/eva/topics/gmail"


def connector(connector_id: UUID = CONNECTOR_ID) -> ConnectorRecord:
    return ConnectorRecord(
        id=connector_id,
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        provider="gmail",
        account_identity=f"owner-{connector_id}@example.com",
        granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        status=ConnectorStatus.ACTIVE,
        secret_reference=f"projects/eva/secrets/{connector_id}",
        connected_at=NOW - timedelta(days=1),
    )


def sync_record(
    connector_id: UUID = CONNECTOR_ID,
    *,
    renewal_at: datetime | None = None,
    safety_at: datetime | None = None,
) -> GmailSyncRecord:
    return GmailSyncRecord(
        connector_account_id=connector_id,
        history_id="100",
        watch_expiration=NOW + timedelta(days=7),
        last_notification_at=NOW - timedelta(hours=2),
        last_successful_sync_at=NOW - timedelta(hours=2),
        next_watch_renewal_at=renewal_at or NOW + timedelta(days=1),
        next_safety_sync_at=safety_at or NOW + timedelta(hours=1),
        claim_id=None,
        lease_expires_at=None,
    )


class FakeRepository:
    def __init__(self) -> None:
        self.records = {
            CONNECTOR_ID: connector(),
            OTHER_CONNECTOR_ID: connector(OTHER_CONNECTOR_ID),
        }
        self.states = {
            CONNECTOR_ID: sync_record(),
            OTHER_CONNECTOR_ID: sync_record(OTHER_CONNECTOR_ID),
        }
        self.due_ids: tuple[UUID, ...] = (CONNECTOR_ID,)
        self.busy: set[UUID] = set()
        self.claims: dict[UUID, SyncClaim] = {}
        self.claim_failure: BaseException | None = None
        self.renewal_failure: BaseException | None = None
        self.release_failure: BaseException | None = None
        self.transition_failure: BaseException | None = None
        self.calls: list[str] = []

    async def due_for_maintenance(self, now: datetime) -> tuple[UUID, ...]:
        self.calls.append("due")
        return self.due_ids

    async def get_sync_state(self, connector_id: UUID) -> GmailSyncRecord | None:
        self.calls.append(f"state:{connector_id}")
        return self.states.get(connector_id)

    async def claim_sync(
        self, connector_id: UUID, now: datetime, lease_seconds: int
    ) -> SyncClaim | None:
        self.calls.append(f"claim:{connector_id}")
        if self.claim_failure is not None:
            raise self.claim_failure
        if connector_id in self.busy:
            return None
        claim = SyncClaim(
            claim_id=uuid7(),
            connector=self.records[connector_id],
            sync=self.states[connector_id],
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
        self.claims[connector_id] = claim
        self.busy.add(connector_id)
        return claim

    async def record_watch_renewal(
        self, claim: SyncClaim, expiration: datetime, next_renewal_at: datetime
    ) -> bool:
        connector_id = claim.connector.id
        self.calls.append(f"renew:{connector_id}")
        if self.renewal_failure is not None:
            raise self.renewal_failure
        if self.claims.get(connector_id) != claim:
            return False
        self.states[connector_id] = self.states[connector_id].model_copy(
            update={
                "watch_expiration": expiration,
                "next_watch_renewal_at": next_renewal_at,
                "claim_id": None,
                "lease_expires_at": None,
            }
        )
        self.busy.discard(connector_id)
        return True

    async def release_sync(self, claim: SyncClaim) -> bool:
        connector_id = claim.connector.id
        self.calls.append(f"release:{connector_id}")
        if self.release_failure is not None:
            raise self.release_failure
        self.busy.discard(connector_id)
        return True

    async def mark_reauthorization_required(self, connector_id: UUID, error: BaseException) -> None:
        self.calls.append(f"reauthorize:{connector_id}")
        if self.transition_failure is not None:
            raise self.transition_failure
        self.records[connector_id] = self.records[connector_id].model_copy(
            update={"status": ConnectorStatus.REAUTHORIZATION_REQUIRED}
        )
        self.busy.discard(connector_id)


class FakeCredentialStore:
    def __init__(self) -> None:
        self.failure_by_reference: dict[str, BaseException] = {}
        self.references: list[str] = []

    async def get(self, secret_reference: str) -> str:
        self.references.append(secret_reference)
        failure = self.failure_by_reference.get(secret_reference)
        if failure is not None:
            raise failure
        return AUTHORIZED_USER_JSON

    async def put(self, connector_id: UUID, authorized_user_json: str) -> str:
        raise AssertionError("maintenance must not write credentials")


class FakeGmailClient:
    def __init__(self) -> None:
        self.watch_result = WatchResult("999", NOW + timedelta(days=7))
        self.watch_calls: list[str] = []

    async def watch(self, topic_name: str) -> WatchResult:
        self.watch_calls.append(topic_name)
        return self.watch_result

    async def get_profile(self) -> str:
        raise AssertionError("maintenance must not load profile")

    async def list_history(self, start_history_id: str, page_token: str | None) -> HistoryPage:
        raise AssertionError("renewal must not synchronize history")

    async def get_message(self, message_id: str) -> Mapping[str, object]:
        raise AssertionError("renewal must not fetch messages")

    async def list_message_ids(self, query: str, page_token: str | None) -> MessageListPage:
        raise AssertionError("renewal must not recover history")


class FakeFactory:
    def __init__(self) -> None:
        self.clients: list[FakeGmailClient] = []

    async def create(self, authorized_user_json: str) -> GmailClient:
        client = FakeGmailClient()
        self.clients.append(client)
        return cast(GmailClient, client)


class RecordingSyncService:
    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository
        self.results: dict[UUID, SyncResult | BaseException] = {}
        self.calls: list[UUID] = []

    async def sync_connector(self, connector_id: UUID) -> SyncResult:
        self.calls.append(connector_id)
        assert connector_id not in self.repository.busy
        result = self.results.get(
            connector_id,
            SyncResult(SyncStatus.SYNCED, connector_id, 0, "100"),
        )
        if isinstance(result, BaseException):
            raise result
        state = self.repository.states[connector_id]
        self.repository.states[connector_id] = state.model_copy(
            update={"next_safety_sync_at": NOW + timedelta(minutes=60)}
        )
        return result


class Harness:
    def __init__(self) -> None:
        self.repository = FakeRepository()
        self.credentials = FakeCredentialStore()
        self.factory = FakeFactory()
        self.sync = RecordingSyncService(self.repository)
        self.service = GmailMaintenanceService(
            repository=cast(ConnectorRepository, self.repository),
            credential_store=cast(CredentialStore, self.credentials),
            client_factory=cast(GmailClientFactory, self.factory),
            sync_service=cast(GmailSyncService, self.sync),
            topic_name=TOPIC_NAME,
            lease_seconds=300,
        )


async def test_startup_run_renews_due_watch_without_replacing_durable_cursor() -> None:
    """Fails if persisted startup work is skipped or the renewal watch cursor is stored."""
    harness = Harness()
    harness.repository.states[CONNECTOR_ID] = sync_record(renewal_at=NOW)

    summary = await harness.service.run_due(NOW)

    assert summary == MaintenanceSummary(renewed=1, safety_synced=0, failed=0)
    state = harness.repository.states[CONNECTOR_ID]
    assert state.history_id == "100"
    assert state.watch_expiration == NOW + timedelta(days=7)
    assert state.next_watch_renewal_at == NOW + timedelta(hours=24)
    assert state.claim_id is None and state.lease_expires_at is None
    assert harness.factory.clients[0].watch_calls == [TOPIC_NAME]


async def test_run_due_rechecks_persisted_timestamps_and_does_no_early_work() -> None:
    """Fails if a stale due listing triggers provider work before persisted timestamps."""
    harness = Harness()

    summary = await harness.service.run_due(NOW)

    assert summary == MaintenanceSummary(renewed=0, safety_synced=0, failed=0)
    assert harness.credentials.references == []
    assert harness.sync.calls == []


async def test_due_renewal_releases_its_claim_before_due_safety_sync() -> None:
    """Fails if renewal nests the safety claim or an in-memory timer owns repair."""
    harness = Harness()
    harness.repository.states[CONNECTOR_ID] = sync_record(
        renewal_at=NOW,
        safety_at=NOW,
    )

    summary = await harness.service.run_due(NOW)

    assert summary == MaintenanceSummary(renewed=1, safety_synced=1, failed=0)
    assert harness.sync.calls == [CONNECTOR_ID]
    assert harness.repository.states[CONNECTOR_ID].next_safety_sync_at == NOW + timedelta(
        minutes=60
    )
    claim_index = harness.repository.calls.index(f"claim:{CONNECTOR_ID}")
    renewal_index = harness.repository.calls.index(f"renew:{CONNECTOR_ID}")
    assert claim_index < renewal_index


async def test_notification_claim_blocks_both_due_actions_without_provider_work() -> None:
    """Fails if maintenance bypasses the synchronization lease held by notification work."""
    harness = Harness()
    harness.repository.states[CONNECTOR_ID] = sync_record(
        renewal_at=NOW,
        safety_at=NOW,
    )
    harness.repository.busy.add(CONNECTOR_ID)
    harness.sync.results[CONNECTOR_ID] = SyncResult(
        SyncStatus.BUSY,
        CONNECTOR_ID,
        0,
        "100",
    )

    summary = await harness.service.run_due(NOW)

    assert summary == MaintenanceSummary(renewed=0, safety_synced=0, failed=2)
    assert harness.factory.clients == []
    assert harness.sync.calls == [CONNECTOR_ID]


async def test_failure_on_one_connector_does_not_stop_later_due_work() -> None:
    """Fails if one provider failure prevents persisted due work for other connectors."""
    harness = Harness()
    harness.repository.due_ids = (CONNECTOR_ID, OTHER_CONNECTOR_ID)
    harness.repository.states[CONNECTOR_ID] = sync_record(renewal_at=NOW)
    harness.repository.states[OTHER_CONNECTOR_ID] = sync_record(
        OTHER_CONNECTOR_ID,
        renewal_at=NOW,
    )
    first_reference = harness.repository.records[CONNECTOR_ID].secret_reference
    assert first_reference is not None
    harness.credentials.failure_by_reference[first_reference] = RuntimeError(
        "private-owner@example.com refresh-token"
    )

    summary = await harness.service.run_due(NOW)

    assert summary == MaintenanceSummary(renewed=1, safety_synced=0, failed=1)
    assert len(harness.factory.clients) == 1
    assert CONNECTOR_ID not in harness.repository.busy
    assert harness.repository.states[CONNECTOR_ID].history_id == "100"


async def test_renewal_revocation_alone_transitions_reauthorization() -> None:
    """Fails if revoked maintenance credentials hot-retry or lose the durable cursor."""
    harness = Harness()
    harness.repository.states[CONNECTOR_ID] = sync_record(renewal_at=NOW)
    reference = harness.repository.records[CONNECTOR_ID].secret_reference
    assert reference is not None
    revoked = AuthorizationRevoked("Google authorization has been revoked")
    harness.credentials.failure_by_reference[reference] = revoked

    summary = await harness.service.run_due(NOW)

    assert summary == MaintenanceSummary(renewed=0, safety_synced=0, failed=1)
    assert (
        harness.repository.records[CONNECTOR_ID].status == ConnectorStatus.REAUTHORIZATION_REQUIRED
    )
    assert harness.repository.states[CONNECTOR_ID].history_id == "100"
    assert CONNECTOR_ID not in harness.repository.busy


async def test_renewal_cancellation_best_effort_releases_and_propagates_unchanged() -> None:
    """Fails if worker shutdown is counted/swallowed or strands maintenance's claim."""
    harness = Harness()
    harness.repository.states[CONNECTOR_ID] = sync_record(renewal_at=NOW)
    reference = harness.repository.records[CONNECTOR_ID].secret_reference
    assert reference is not None
    cancellation = asyncio.CancelledError("cancelled-renewal")
    harness.credentials.failure_by_reference[reference] = cancellation

    with pytest.raises(asyncio.CancelledError) as raised:
        await harness.service.run_due(NOW)

    assert raised.value is cancellation
    assert CONNECTOR_ID not in harness.repository.busy
    assert harness.repository.calls[-1] == f"release:{CONNECTOR_ID}"
