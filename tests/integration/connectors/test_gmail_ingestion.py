from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import func, select

from eva_ai.connectors.gmail.contracts import (
    CredentialStore,
    GmailClient,
    GmailClientFactory,
    GmailNotification,
    HistoryPage,
)
from eva_ai.connectors.gmail.sync import (
    GmailRecoveryService,
    GmailSyncError,
    GmailSyncService,
    SyncStatus,
)
from eva_ai.connectors.repository import ConnectorRepository
from eva_ai.connectors.types import ConnectorStatus, SyncClaim
from eva_ai.db import Database
from eva_ai.db.models import Event, EventProcessing, GmailSyncState, OutboxMessage
from eva_ai.events.service import EventService, IngestResult
from eva_ai.events.types import NewEvent
from tests.integration.factories import create_scope, gmail_watch

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)


async def assert_connector_transaction_closed(database: Database, connector_id: UUID) -> None:
    async with database.session() as session:
        async with session.begin():
            row = await session.scalar(
                select(GmailSyncState)
                .where(GmailSyncState.connector_account_id == connector_id)
                .with_for_update(nowait=True)
            )
            assert row is not None


class CheckingCredentialStore:
    def __init__(self, database: Database, connector_id: UUID) -> None:
        self.database = database
        self.connector_id = connector_id

    async def get(self, secret_reference: str) -> str:
        await assert_connector_transaction_closed(self.database, self.connector_id)
        return '{"type":"authorized_user","refresh_token":"synthetic"}'

    async def put(self, connector_id: UUID, authorized_user_json: str) -> str:
        raise AssertionError("synchronization must not write credentials")


class UnusedCredentialStore:
    async def get(self, secret_reference: str) -> str:
        raise AssertionError("status-only notification must not load credentials")

    async def put(self, connector_id: UUID, authorized_user_json: str) -> str:
        raise AssertionError("synchronization must not write credentials")


class UnusedClientFactory:
    async def create(self, authorized_user_json: str) -> GmailClient:
        raise AssertionError("status-only notification must not construct Gmail client")


class UnusedEventService:
    async def ingest(self, command: NewEvent) -> IngestResult:
        raise AssertionError("status-only notification must not ingest")


class CheckingGmailClient:
    def __init__(self, database: Database, connector_id: UUID) -> None:
        self.database = database
        self.connector_id = connector_id

    async def list_history(self, start_history_id: str, page_token: str | None) -> HistoryPage:
        await assert_connector_transaction_closed(self.database, self.connector_id)
        assert start_history_id == "100" and page_token is None
        return HistoryPage(("message-1",), "101", None)

    async def get_message(self, message_id: str) -> Mapping[str, object]:
        await assert_connector_transaction_closed(self.database, self.connector_id)
        assert message_id == "message-1"
        return {
            "id": message_id,
            "threadId": "thread-1",
            "internalDate": str(int(NOW.timestamp() * 1000)),
            "labelIds": ["INBOX", "CATEGORY_PRIMARY"],
            "snippet": "private message content",
            "payload": {
                "mimeType": "text/plain",
                "headers": [],
                "body": {"size": 0},
            },
        }

    async def get_profile(self) -> str:
        raise AssertionError("synchronization must not load profile")

    async def watch(self, topic_name: str) -> object:
        raise AssertionError("forward synchronization must not renew watch")

    async def list_message_ids(self, query: str, page_token: str | None) -> object:
        raise AssertionError("forward synchronization must not scan mailbox")

    async def close(self) -> None:
        return None


class CheckingFactory:
    def __init__(self, database: Database, connector_id: UUID) -> None:
        self.database = database
        self.connector_id = connector_id
        self.gmail = CheckingGmailClient(database, connector_id)

    async def create(self, authorized_user_json: str) -> GmailClient:
        await assert_connector_transaction_closed(self.database, self.connector_id)
        return cast(GmailClient, self.gmail)


class CheckingEventService:
    def __init__(self, database: Database, connector_id: UUID) -> None:
        self.database = database
        self.connector_id = connector_id
        self.real = EventService(database, "eva-events")

    async def ingest(self, command: NewEvent) -> IngestResult:
        await assert_connector_transaction_closed(self.database, self.connector_id)
        return await self.real.ingest(command)


class FailCompletionOnceRepository:
    def __init__(self, real: ConnectorRepository) -> None:
        self.real = real
        self.failures = 1

    def __getattr__(self, name: str) -> object:
        return getattr(self.real, name)

    async def complete_sync(
        self,
        claim: SyncClaim,
        history_id: str,
        now: datetime,
        next_safety_sync_at: datetime,
    ) -> bool:
        if self.failures:
            self.failures -= 1
            return False
        return await self.real.complete_sync(
            claim,
            history_id,
            now,
            next_safety_sync_at,
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("status", "expected_status", "expected_history_id"),
    [
        (ConnectorStatus.CONNECTING, SyncStatus.CONNECTING, None),
        (ConnectorStatus.ERROR, SyncStatus.CONNECTING, "100"),
        (
            ConnectorStatus.REAUTHORIZATION_REQUIRED,
            SyncStatus.REAUTHORIZATION_REQUIRED,
            "100",
        ),
        (ConnectorStatus.ACTIVE, SyncStatus.ALREADY_COVERED, "100"),
    ],
)
async def test_known_status_outcomes_durably_record_notification_before_returning(
    database: Database,
    status: ConnectorStatus,
    expected_status: SyncStatus,
    expected_history_id: str | None,
) -> None:
    """Fails if a known terminal/covered outcome returns before PostgreSQL observes it."""
    scope = await create_scope(database)
    identity = f"status-{status.value.lower()}+{scope.workspace_id}@example.com"
    repository = ConnectorRepository(database)
    reserved = await repository.reserve_gmail(
        scope.user_id,
        scope.workspace_id,
        identity,
        ("https://www.googleapis.com/auth/gmail.readonly",),
        NOW,
    )
    if status != ConnectorStatus.CONNECTING:
        await repository.attach_secret(reserved.id, "projects/eva/secrets/status/versions/1")
        await repository.activate_initial_watch(
            reserved.id,
            gmail_watch("100", NOW + timedelta(days=7)),
            NOW,
            NOW + timedelta(days=1),
            NOW + timedelta(hours=1),
        )
    if status == ConnectorStatus.ERROR:
        await repository.mark_error(reserved.id, RuntimeError("synthetic provider failure"))
    elif status == ConnectorStatus.REAUTHORIZATION_REQUIRED:
        await repository.mark_reauthorization_required(
            reserved.id,
            RuntimeError("synthetic authorization failure"),
        )
    service = GmailSyncService(
        repository=repository,
        credential_store=cast(CredentialStore, UnusedCredentialStore()),
        client_factory=cast(GmailClientFactory, UnusedClientFactory()),
        event_service=cast(EventService, UnusedEventService()),
        clock=lambda: NOW + timedelta(minutes=5),
        lease_seconds=300,
        recovery_service=GmailRecoveryService(
            repository=repository,
            event_service=cast(EventService, UnusedEventService()),
            topic_name="projects/eva/topics/gmail",
        ),
    )

    result = await service.handle(GmailNotification(identity, "100"))

    state = await repository.get_sync_state(reserved.id)
    assert result.status == expected_status
    assert result.final_history_id == expected_history_id
    assert state is not None
    assert state.last_notification_at == NOW + timedelta(minutes=5)
    assert state.history_id == expected_history_id
    assert state.claim_id is None and state.lease_expires_at is None


@pytest.mark.integration
async def test_crash_replay_creates_one_backbone_and_advances_cursor_after_retry(
    database: Database,
) -> None:
    """Fails if replay duplicates the backbone or provider work holds the claim transaction."""
    scope = await create_scope(database)
    identity = f"gmail-ingestion+{scope.workspace_id}@example.com"
    real_repository = ConnectorRepository(database)
    reserved = await real_repository.reserve_gmail(
        scope.user_id,
        scope.workspace_id,
        identity,
        ("https://www.googleapis.com/auth/gmail.readonly",),
        NOW,
    )
    await real_repository.attach_secret(
        reserved.id, "projects/eva/secrets/gmail-ingestion/versions/1"
    )
    await real_repository.activate_initial_watch(
        reserved.id,
        gmail_watch("100", NOW + timedelta(days=7)),
        NOW,
        NOW + timedelta(days=1),
        NOW + timedelta(hours=1),
    )
    repository = FailCompletionOnceRepository(real_repository)
    event_service = CheckingEventService(database, reserved.id)
    service = GmailSyncService(
        repository=cast(ConnectorRepository, repository),
        credential_store=cast(CredentialStore, CheckingCredentialStore(database, reserved.id)),
        client_factory=cast(GmailClientFactory, CheckingFactory(database, reserved.id)),
        event_service=cast(EventService, event_service),
        clock=lambda: NOW,
        lease_seconds=300,
        recovery_service=GmailRecoveryService(
            repository=cast(ConnectorRepository, repository),
            event_service=cast(EventService, event_service),
            topic_name="projects/eva/topics/gmail",
        ),
    )
    notification = GmailNotification(identity, "101")

    with pytest.raises(GmailSyncError, match="claim is no longer current"):
        await service.handle(notification)

    cursor_after_failure = await real_repository.get_sync_state(reserved.id)
    assert cursor_after_failure is not None and cursor_after_failure.history_id == "100"

    result = await service.handle(notification)

    async with database.session() as session:
        event_count = await session.scalar(
            select(func.count()).select_from(Event).where(Event.workspace_id == scope.workspace_id)
        )
        processing_count = await session.scalar(
            select(func.count())
            .select_from(EventProcessing)
            .join(Event, Event.id == EventProcessing.event_id)
            .where(Event.workspace_id == scope.workspace_id)
        )
        outbox_count = await session.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .join(Event, Event.id == OutboxMessage.event_id)
            .where(Event.workspace_id == scope.workspace_id)
        )
        counts = (event_count, processing_count, outbox_count)
    final_state = await real_repository.get_sync_state(reserved.id)
    assert result.status == SyncStatus.SYNCED
    assert result.events_created == 0
    assert result.final_history_id == "101"
    assert counts == (1, 1, 1)
    assert final_state is not None and final_state.history_id == "101"
    assert final_state.last_notification_at == NOW
    assert final_state.claim_id is None and final_state.lease_expires_at is None
