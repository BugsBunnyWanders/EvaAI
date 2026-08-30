from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import func, select

from eva_ai.connectors.gmail.bootstrap import ConnectGmail, GmailBootstrapService
from eva_ai.connectors.gmail.contracts import (
    AuthorizedUserGrant,
    CredentialStore,
    GmailClient,
    GmailClientFactory,
    GmailNotification,
    GmailProfile,
    HistoryPage,
    OAuthAuthorizer,
    WatchResult,
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


class BootstrapCredentialStore:
    def __init__(self) -> None:
        self.value = '{"type":"authorized_user","refresh_token":"synthetic"}'

    async def put(self, connector_id: UUID, authorized_user_json: str) -> str:
        assert authorized_user_json == self.value
        return f"projects/eva/secrets/{connector_id}"

    async def get(self, secret_reference: str) -> str:
        assert secret_reference.startswith("projects/eva/secrets/")
        return self.value


class StaticAuthorizer:
    async def authorize(self, client_file: Path, scopes: tuple[str, ...]) -> AuthorizedUserGrant:
        del client_file, scopes
        return AuthorizedUserGrant(
            authorized_user_json='{"type":"authorized_user","refresh_token":"synthetic"}'
        )


class BootstrapGmailClient:
    def __init__(self, profile: GmailProfile, watch: WatchResult) -> None:
        self.profile = profile
        self.watch_result = watch
        self.watch_calls = 0

    async def get_profile(self) -> GmailProfile:
        return self.profile

    async def watch(self, topic_name: str) -> WatchResult:
        assert topic_name == "projects/eva/topics/gmail"
        self.watch_calls += 1
        return self.watch_result

    async def list_history(self, start_history_id: str, page_token: str | None) -> HistoryPage:
        raise AssertionError("bootstrap must not list history")

    async def get_message(self, message_id: str) -> Mapping[str, object]:
        raise AssertionError("bootstrap must not fetch messages")

    async def list_message_ids(self, query: str, page_token: str | None) -> object:
        raise AssertionError("bootstrap must not scan messages")

    async def close(self) -> None:
        return None


class SequenceClientFactory:
    def __init__(self, clients: list[GmailClient]) -> None:
        self.clients = clients

    async def create(self, authorized_user_json: str) -> GmailClient:
        assert "synthetic" in authorized_user_json
        return self.clients.pop(0)


class FailInitialActivationOnceRepository:
    def __init__(self, real: ConnectorRepository) -> None:
        self.real = real
        self.failures = 1

    def __getattr__(self, name: str) -> object:
        return getattr(self.real, name)

    async def activate_initial_watch(
        self,
        connector_id: UUID,
        watch: WatchResult,
        next_renewal_at: datetime,
        next_safety_sync_at: datetime,
    ) -> object:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("synthetic activation transaction failure")
        return await self.real.activate_initial_watch(
            connector_id,
            watch,
            next_renewal_at,
            next_safety_sync_at,
        )


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

    async def get_profile(self) -> GmailProfile:
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
        await repository.prepare_initial_watch(reserved.id, "100", NOW)
        await repository.activate_initial_watch(
            reserved.id,
            gmail_watch("100", NOW + timedelta(days=7)),
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
    await real_repository.prepare_initial_watch(reserved.id, "100", NOW)
    await real_repository.activate_initial_watch(
        reserved.id,
        gmail_watch("100", NOW + timedelta(days=7)),
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


@pytest.mark.integration
async def test_successful_initial_watch_activation_crash_replays_from_profile_boundary_once(
    database: Database,
) -> None:
    """Fails if retry replaces the only cursor able to discover post-watch crash mail."""
    scope = await create_scope(database)
    identity = f"bootstrap-crash+{scope.workspace_id}@example.com"
    real_repository = ConnectorRepository(database)
    repository = FailInitialActivationOnceRepository(real_repository)
    credential_store = BootstrapCredentialStore()
    first_client = BootstrapGmailClient(
        GmailProfile(identity, "100"),
        gmail_watch("150", NOW + timedelta(days=7)),
    )
    second_client = BootstrapGmailClient(
        GmailProfile(identity, "200"),
        gmail_watch("250", NOW + timedelta(days=7, minutes=1)),
    )
    sync_client = CheckingGmailClient(database, UUID(int=0))
    factory = SequenceClientFactory(
        [
            cast(GmailClient, first_client),
            cast(GmailClient, second_client),
            cast(GmailClient, sync_client),
        ]
    )
    clock_values = iter(
        (
            NOW,
            NOW + timedelta(seconds=1),
            NOW + timedelta(minutes=1),
            NOW + timedelta(minutes=1, seconds=1),
        )
    )
    bootstrap = GmailBootstrapService(
        repository=cast(ConnectorRepository, repository),
        authorizer=cast(OAuthAuthorizer, StaticAuthorizer()),
        credential_store=cast(CredentialStore, credential_store),
        client_factory=cast(GmailClientFactory, factory),
        clock=lambda: next(clock_values),
    )
    command = ConnectGmail(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        expected_identity=identity,
        client_file=Path("synthetic-client.json"),
        topic_name="projects/eva/topics/gmail",
    )

    with pytest.raises(RuntimeError, match="activation transaction failure"):
        await bootstrap.connect(command)

    failed_connector = await real_repository.find_by_identity(identity)
    assert failed_connector is not None
    failed_state = await real_repository.get_sync_state(failed_connector.id)
    assert failed_connector.status == ConnectorStatus.ERROR
    assert failed_state is not None and failed_state.history_id == "100"
    sync_client.connector_id = failed_connector.id

    active = await bootstrap.connect(command)
    activated_state = await real_repository.get_sync_state(active.id)
    assert active.status == ConnectorStatus.ACTIVE
    assert active.connected_at == NOW
    assert activated_state is not None and activated_state.history_id == "100"
    assert first_client.watch_calls == second_client.watch_calls == 1

    event_service = CheckingEventService(database, active.id)
    sync_service = GmailSyncService(
        repository=real_repository,
        credential_store=cast(CredentialStore, credential_store),
        client_factory=cast(GmailClientFactory, factory),
        event_service=cast(EventService, event_service),
        clock=lambda: NOW + timedelta(minutes=2),
        lease_seconds=300,
        recovery_service=GmailRecoveryService(
            repository=real_repository,
            event_service=cast(EventService, event_service),
            topic_name="projects/eva/topics/gmail",
        ),
    )
    notification = GmailNotification(identity, "101")

    first_sync = await sync_service.handle(notification)
    second_sync = await sync_service.handle(notification)

    async with database.session() as session:
        counts = (
            await session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.workspace_id == scope.workspace_id)
            ),
            await session.scalar(
                select(func.count())
                .select_from(EventProcessing)
                .join(Event, Event.id == EventProcessing.event_id)
                .where(Event.workspace_id == scope.workspace_id)
            ),
            await session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .join(Event, Event.id == OutboxMessage.event_id)
                .where(Event.workspace_id == scope.workspace_id)
            ),
        )
    assert first_sync.status == SyncStatus.SYNCED
    assert second_sync.status == SyncStatus.ALREADY_COVERED
    assert counts == (1, 1, 1)
