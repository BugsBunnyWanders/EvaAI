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
    GmailNotification,
    HistoryCursorExpired,
    HistoryPage,
)
from eva_ai.connectors.gmail.sync import (
    GmailSyncError,
    GmailSyncService,
    SyncResult,
    SyncStatus,
)
from eva_ai.connectors.repository import AmbiguousConnectorIdentity, ConnectorRepository
from eva_ai.connectors.types import (
    ConnectorRecord,
    ConnectorStatus,
    GmailSyncRecord,
    SyncClaim,
)
from eva_ai.events.service import EventService, IngestResult
from eva_ai.events.types import NewEvent
from eva_ai.integrations.gmail.api import GmailProviderError

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)
USER_ID = UUID("0191cafe-7b00-7000-8000-000000000001")
WORKSPACE_ID = UUID("0191cafe-7b00-7000-8000-000000000002")
CONNECTOR_ID = UUID("0191cafe-7b00-7000-8000-000000000003")
SECRET_REFERENCE = "projects/eva/secrets/gmail/connector"
AUTHORIZED_USER_JSON = '{"type":"authorized_user","refresh_token":"synthetic"}'


def connector(status: ConnectorStatus = ConnectorStatus.ACTIVE) -> ConnectorRecord:
    return ConnectorRecord(
        id=CONNECTOR_ID,
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        provider="gmail",
        account_identity="owner@example.com",
        granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        status=status,
        secret_reference=SECRET_REFERENCE,
        connected_at=NOW,
    )


def sync_record(history_id: str = "100") -> GmailSyncRecord:
    return GmailSyncRecord(
        connector_account_id=CONNECTOR_ID,
        history_id=history_id,
        watch_expiration=NOW + timedelta(days=7),
        last_notification_at=None,
        last_successful_sync_at=None,
        next_watch_renewal_at=NOW + timedelta(days=1),
        next_safety_sync_at=NOW + timedelta(hours=1),
        claim_id=None,
        lease_expires_at=None,
    )


def raw_message(
    message_id: str,
    occurred_at: datetime,
    *,
    labels: list[str] | None = None,
) -> Mapping[str, object]:
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "internalDate": str(int(occurred_at.timestamp() * 1000)),
        "labelIds": ["INBOX"] if labels is None else labels,
        "snippet": f"snippet-{message_id}",
        "payload": {"mimeType": "text/plain", "headers": [], "body": {"size": 0}},
    }


class FakeRepository:
    def __init__(
        self,
        *,
        record: ConnectorRecord | None = None,
        state: GmailSyncRecord | None = None,
    ) -> None:
        self.record: ConnectorRecord | None = record if record is not None else connector()
        self.state = state if state is not None else sync_record()
        self.busy = False
        self.ambiguous = False
        self.complete_failures = 0
        self.transition_failure: Exception | None = None
        self.claim: SyncClaim | None = None
        self.marked_error: BaseException | None = None
        self.calls: list[str] = []

    async def find_by_identity(self, account_identity: str) -> ConnectorRecord | None:
        self.calls.append("find")
        if self.ambiguous:
            raise AmbiguousConnectorIdentity("Gmail identity maps to multiple connectors")
        if self.record is None or account_identity.lower() != self.record.account_identity:
            return None
        return self.record

    async def get(self, connector_id: UUID) -> ConnectorRecord | None:
        self.calls.append("get")
        return self.record if self.record is not None and self.record.id == connector_id else None

    async def get_sync_state(self, connector_id: UUID) -> GmailSyncRecord | None:
        self.calls.append("get_sync_state")
        return self.state if self.state is not None and connector_id == CONNECTOR_ID else None

    async def claim_sync(
        self, connector_id: UUID, now: datetime, lease_seconds: int
    ) -> SyncClaim | None:
        self.calls.append("claim")
        if self.busy or self.record is None or self.record.status != ConnectorStatus.ACTIVE:
            return None
        assert self.state is not None and connector_id == self.record.id
        self.busy = True
        self.claim = SyncClaim(
            claim_id=uuid7(),
            connector=self.record,
            sync=self.state,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
        return self.claim

    async def complete_sync(
        self,
        claim: SyncClaim,
        history_id: str,
        now: datetime,
        next_safety_sync_at: datetime,
    ) -> bool:
        self.calls.append("complete")
        if self.complete_failures:
            self.complete_failures -= 1
            return False
        assert claim == self.claim and self.state is not None
        self.state = self.state.model_copy(
            update={
                "history_id": history_id,
                "last_successful_sync_at": now,
                "next_safety_sync_at": next_safety_sync_at,
                "claim_id": None,
                "lease_expires_at": None,
            }
        )
        self.busy = False
        return True

    async def release_sync(self, claim: SyncClaim) -> bool:
        self.calls.append("release")
        if claim != self.claim:
            return False
        self.busy = False
        return True

    async def mark_reauthorization_required(self, connector_id: UUID, error: BaseException) -> None:
        self.calls.append("mark_reauthorization")
        if self.transition_failure is not None:
            raise self.transition_failure
        assert self.record is not None and connector_id == self.record.id
        self.marked_error = error
        self.record = self.record.model_copy(
            update={"status": ConnectorStatus.REAUTHORIZATION_REQUIRED}
        )
        self.busy = False


class FakeCredentialStore:
    def __init__(self) -> None:
        self.failure: Exception | None = None
        self.references: list[str] = []

    async def get(self, secret_reference: str) -> str:
        self.references.append(secret_reference)
        if self.failure is not None:
            raise self.failure
        return AUTHORIZED_USER_JSON

    async def put(self, connector_id: UUID, authorized_user_json: str) -> str:
        raise AssertionError("synchronization must not write credentials")


class FakeGmailClient:
    def __init__(self) -> None:
        self.pages: dict[str | None, HistoryPage] = {
            None: HistoryPage(message_ids=(), history_id="100", next_page_token=None)
        }
        self.messages: dict[str, Mapping[str, object]] = {}
        self.failure: Exception | None = None
        self.history_calls: list[tuple[str, str | None]] = []
        self.message_calls: list[str] = []

    async def list_history(self, start_history_id: str, page_token: str | None) -> HistoryPage:
        self.history_calls.append((start_history_id, page_token))
        if self.failure is not None:
            raise self.failure
        return self.pages[page_token]

    async def get_message(self, message_id: str) -> Mapping[str, object]:
        self.message_calls.append(message_id)
        if self.failure is not None:
            raise self.failure
        return self.messages[message_id]

    async def get_profile(self) -> str:
        raise AssertionError("synchronization must not load the Gmail profile")

    async def watch(self, topic_name: str) -> object:
        raise AssertionError("forward synchronization must not renew the watch")

    async def list_message_ids(self, query: str, page_token: str | None) -> object:
        raise AssertionError("forward synchronization must not scan the mailbox")


class FakeGmailClientFactory:
    def __init__(self, gmail: FakeGmailClient) -> None:
        self.gmail = gmail
        self.failure: Exception | None = None
        self.credentials: list[str] = []

    async def create(self, authorized_user_json: str) -> GmailClient:
        self.credentials.append(authorized_user_json)
        if self.failure is not None:
            raise self.failure
        return cast(GmailClient, self.gmail)


class RecordingEventService:
    def __init__(self) -> None:
        self.by_key: dict[str, IngestResult] = {}
        self.events: list[NewEvent] = []

    async def ingest(self, command: NewEvent) -> IngestResult:
        self.events.append(command)
        existing = self.by_key.get(command.idempotency_key)
        if existing is not None:
            return IngestResult(existing.event_id, created=False)
        result = IngestResult(command.id, created=True)
        self.by_key[command.idempotency_key] = result
        return result


class Harness:
    def __init__(
        self,
        *,
        record: ConnectorRecord | None = None,
        state: GmailSyncRecord | None = None,
    ) -> None:
        self.repository = FakeRepository(record=record, state=state)
        self.credentials = FakeCredentialStore()
        self.gmail = FakeGmailClient()
        self.factory = FakeGmailClientFactory(self.gmail)
        self.events = RecordingEventService()
        self.service = GmailSyncService(
            repository=cast(ConnectorRepository, self.repository),
            credential_store=cast(CredentialStore, self.credentials),
            client_factory=cast(GmailClientFactory, self.factory),
            event_service=cast(EventService, self.events),
            clock=lambda: NOW,
            lease_seconds=300,
        )

    async def handle(self, history_id: str = "101") -> SyncResult:
        return await self.service.handle(GmailNotification("owner@example.com", history_id))


async def test_sync_pages_from_durable_cursor_deduplicates_and_filters_current_messages() -> None:
    """Fails on notification-cursor use, incomplete paging, reordered dedupe, or stale filters."""
    harness = Harness()
    harness.gmail.pages = {
        None: HistoryPage(
            message_ids=("message-1", "removed", "old"),
            history_id="103",
            next_page_token="page-2",
        ),
        "page-2": HistoryPage(
            message_ids=("message-1", "message-2"),
            history_id="105",
            next_page_token=None,
        ),
    }
    harness.gmail.messages = {
        "message-1": raw_message("message-1", NOW),
        "removed": raw_message("removed", NOW, labels=["CATEGORY_UPDATES"]),
        "old": raw_message("old", NOW - timedelta(milliseconds=1)),
        "message-2": raw_message("message-2", NOW + timedelta(seconds=1)),
    }

    result = await harness.handle(history_id="999")

    assert result == SyncResult(SyncStatus.SYNCED, CONNECTOR_ID, 2, "105")
    assert harness.gmail.history_calls == [("100", None), ("100", "page-2")]
    assert harness.gmail.message_calls == ["message-1", "removed", "old", "message-2"]
    assert [event.external_id for event in harness.events.events] == ["message-1", "message-2"]
    assert harness.repository.state is not None
    assert harness.repository.state.history_id == "105"
    assert harness.repository.state.next_safety_sync_at == NOW + timedelta(minutes=60)


async def test_empty_history_still_durably_advances_to_provider_final_cursor() -> None:
    """Fails if an empty range leaves the durable cursor and repair schedule stale."""
    harness = Harness()
    harness.gmail.pages = {
        None: HistoryPage(message_ids=(), history_id="108", next_page_token=None)
    }

    result = await harness.handle(history_id="108")

    assert result == SyncResult(SyncStatus.SYNCED, CONNECTOR_ID, 0, "108")
    assert harness.gmail.message_calls == []
    assert harness.events.events == []
    assert harness.repository.state is not None
    assert harness.repository.state.history_id == "108"


async def test_already_covered_notification_uses_persisted_cursor_without_provider_work() -> None:
    """Fails if an old wake hint can move the cursor backward or start provider work."""
    harness = Harness(state=sync_record("500"))

    result = await harness.handle(history_id="499")

    assert result == SyncResult(SyncStatus.ALREADY_COVERED, CONNECTOR_ID, 0, "500")
    assert harness.repository.calls == ["find", "get_sync_state"]
    assert harness.credentials.references == []
    assert harness.gmail.history_calls == []


async def test_active_claim_returns_busy_without_loading_credentials() -> None:
    """Fails if two synchronizers can do provider work for one durable cursor."""
    harness = Harness()
    harness.repository.busy = True

    result = await harness.handle()

    assert result == SyncResult(SyncStatus.BUSY, CONNECTOR_ID, 0, "100")
    assert harness.credentials.references == []
    assert harness.gmail.history_calls == []


@pytest.mark.parametrize("status", [ConnectorStatus.CONNECTING, ConnectorStatus.ERROR])
async def test_known_retryable_connector_state_returns_connecting(status: ConnectorStatus) -> None:
    """Fails if an initial-watch race or retryable setup error is terminally acknowledged."""
    harness = Harness(record=connector(status))

    result = await harness.handle()

    assert result == SyncResult(SyncStatus.CONNECTING, CONNECTOR_ID, 0, "100")
    assert harness.credentials.references == []


@pytest.mark.parametrize(
    ("record", "expected_status", "expected_id"),
    [
        (
            connector(ConnectorStatus.REAUTHORIZATION_REQUIRED),
            SyncStatus.REAUTHORIZATION_REQUIRED,
            CONNECTOR_ID,
        ),
        (connector(ConnectorStatus.DISABLED), SyncStatus.UNKNOWN_ACCOUNT, None),
        (None, SyncStatus.UNKNOWN_ACCOUNT, None),
    ],
)
async def test_terminal_or_unknown_connector_never_ingests(
    record: ConnectorRecord | None,
    expected_status: SyncStatus,
    expected_id: UUID | None,
) -> None:
    """Fails if disabled, reauthorization-required, or unknown identities reach Gmail."""
    harness = Harness(record=record)
    if record is None:
        harness.repository.record = None

    result = await harness.handle()

    assert result.status == expected_status
    assert result.connector_id == expected_id
    assert result.events_created == 0
    assert harness.credentials.references == []
    assert harness.gmail.history_calls == []


async def test_ambiguous_identity_fails_closed_without_mailbox_identity_in_error() -> None:
    """Fails if cross-workspace identity ambiguity picks an arbitrary connector."""
    harness = Harness()
    harness.repository.ambiguous = True

    with pytest.raises(AmbiguousConnectorIdentity) as raised:
        await harness.handle()

    assert str(raised.value) == "Gmail identity maps to multiple connectors"
    assert "owner@example.com" not in repr(raised.value)
    assert harness.credentials.references == []


async def test_authorization_revocation_marks_same_exception_and_keeps_cursor() -> None:
    """Fails if revocation is retried, persisted with provider text, or advances history."""
    harness = Harness()
    revoked = AuthorizationRevoked("Google authorization has been revoked")
    harness.credentials.failure = revoked

    result = await harness.handle()

    assert result == SyncResult(SyncStatus.REAUTHORIZATION_REQUIRED, CONNECTOR_ID, 0, "100")
    assert harness.repository.marked_error is revoked
    assert harness.repository.state is not None and harness.repository.state.history_id == "100"
    assert "complete" not in harness.repository.calls


async def test_failed_reauthorization_transition_becomes_chain_free_retryable_error() -> None:
    """Fails if a repository failure masks or exposes revoked credentials as terminal state."""
    harness = Harness()
    harness.credentials.failure = AuthorizationRevoked(
        "Google authorization has been revoked: private-provider-body"
    )
    harness.repository.transition_failure = RuntimeError("private-database-response token=secret")

    with pytest.raises(GmailSyncError) as raised:
        await harness.handle()

    assert str(raised.value) == "Gmail synchronization state transition failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "private-provider-body" not in repr(raised.value)
    assert "private-database-response" not in repr(raised.value)
    assert harness.repository.record is not None
    assert harness.repository.record.status == ConnectorStatus.ACTIVE
    assert harness.repository.busy is False


async def test_transient_provider_error_stays_retryable_and_releases_claim() -> None:
    """Fails if a transient Gmail error becomes reauthorization or keeps a live lease."""
    harness = Harness()
    provider_error = GmailProviderError("Gmail API request failed")
    harness.gmail.failure = provider_error

    with pytest.raises(GmailProviderError) as raised:
        await harness.handle()

    assert raised.value is provider_error
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert harness.repository.marked_error is None
    assert harness.repository.busy is False
    assert harness.repository.state is not None and harness.repository.state.history_id == "100"


async def test_expired_history_propagates_for_recovery_and_releases_claim() -> None:
    """Fails if Task 8 cannot distinguish bounded recovery from transient failure."""
    harness = Harness()
    expired = HistoryCursorExpired("Gmail history cursor expired")
    harness.gmail.failure = expired

    with pytest.raises(HistoryCursorExpired) as raised:
        await harness.handle()

    assert raised.value is expired
    assert harness.repository.busy is False
    assert harness.repository.state is not None and harness.repository.state.history_id == "100"


async def test_invalid_message_error_is_content_free_and_prevents_cursor_advance() -> None:
    """Fails if an unnormalizable message is silently skipped or provider content escapes."""
    harness = Harness()
    harness.gmail.pages = {
        None: HistoryPage(message_ids=("broken",), history_id="101", next_page_token=None)
    }
    harness.gmail.messages = {
        "broken": {
            "id": "broken",
            "threadId": "thread-broken",
            "labelIds": ["INBOX"],
            "subject": "private",
        }
    }

    with pytest.raises(ValueError) as raised:
        await harness.handle()

    assert str(raised.value) == "invalid Gmail message"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "private" not in repr(raised.value)
    assert harness.repository.state is not None and harness.repository.state.history_id == "100"


async def test_crash_style_repeat_reuses_event_and_only_then_advances_cursor() -> None:
    """Fails if Event/cursor transaction separation creates duplicates or skips replay."""
    harness = Harness()
    harness.repository.complete_failures = 1
    harness.gmail.pages = {
        None: HistoryPage(message_ids=("message-1",), history_id="101", next_page_token=None)
    }
    harness.gmail.messages = {"message-1": raw_message("message-1", NOW)}

    with pytest.raises(GmailSyncError, match="claim is no longer current"):
        await harness.handle()

    assert harness.repository.state is not None and harness.repository.state.history_id == "100"
    assert len(harness.events.by_key) == 1

    replay = await harness.handle()

    assert replay == SyncResult(SyncStatus.SYNCED, CONNECTOR_ID, 0, "101")
    assert len(harness.events.events) == 2
    assert len(harness.events.by_key) == 1
    assert harness.repository.state is not None and harness.repository.state.history_id == "101"
