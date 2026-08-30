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
from eva_ai.connectors.gmail.sync import GmailSyncError, GmailSyncService, SyncStatus
from eva_ai.connectors.repository import ConnectorRepository
from eva_ai.connectors.types import SyncClaim
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
    service = GmailSyncService(
        repository=cast(ConnectorRepository, repository),
        credential_store=cast(CredentialStore, CheckingCredentialStore(database, reserved.id)),
        client_factory=cast(GmailClientFactory, CheckingFactory(database, reserved.id)),
        event_service=cast(EventService, CheckingEventService(database, reserved.id)),
        clock=lambda: NOW,
        lease_seconds=300,
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
    assert final_state.claim_id is None and final_state.lease_expires_at is None
