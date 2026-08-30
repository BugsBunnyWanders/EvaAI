import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid7

import pytest
from sqlalchemy.exc import StatementError

from eva_ai.connectors.gmail.contracts import (
    AuthorizationRevoked,
    CredentialStore,
    GmailClient,
    GmailClientFactory,
    GmailNotification,
    GmailProfile,
    HistoryCursorExpired,
    HistoryPage,
    MessageListPage,
    WatchResult,
)
from eva_ai.connectors.gmail.sync import (
    GmailRecoveryService,
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
from eva_ai.integrations.gcp.secret_manager import SecretManagerProviderError
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


def content_bearing_error(marker: str) -> StatementError:
    return StatementError(
        f"database failed with {marker}",
        "SELECT private_data WHERE identity = :identity",
        {"identity": marker, "payload": f"message-{marker}"},
        RuntimeError(f"driver-{marker}"),
    )


def assert_content_free_sync_error(error: GmailSyncError, marker: str) -> None:
    assert str(error) == "Gmail synchronization failed"
    assert marker not in str(error)
    assert marker not in repr(error)
    assert not hasattr(error, "params")
    assert error.__cause__ is None
    assert error.__context__ is None


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
        self.transition_failure: BaseException | None = None
        self.notification_failure: BaseException | None = None
        self.completion_failure: BaseException | None = None
        self.find_failure: BaseException | None = None
        self.get_failure: BaseException | None = None
        self.get_sync_failure: BaseException | None = None
        self.claim_failure: BaseException | None = None
        self.release_failure: BaseException | None = None
        self.claim: SyncClaim | None = None
        self.marked_error: BaseException | None = None
        self.calls: list[str] = []

    async def find_by_identity(self, account_identity: str) -> ConnectorRecord | None:
        self.calls.append("find")
        if self.find_failure is not None:
            raise self.find_failure
        if self.ambiguous:
            raise AmbiguousConnectorIdentity("Gmail identity maps to multiple connectors")
        if self.record is None or account_identity.lower() != self.record.account_identity:
            return None
        return self.record

    async def get(self, connector_id: UUID) -> ConnectorRecord | None:
        self.calls.append("get")
        if self.get_failure is not None:
            raise self.get_failure
        return self.record if self.record is not None and self.record.id == connector_id else None

    async def get_sync_state(self, connector_id: UUID) -> GmailSyncRecord | None:
        self.calls.append("get_sync_state")
        if self.get_sync_failure is not None:
            raise self.get_sync_failure
        return self.state if self.state is not None and connector_id == CONNECTOR_ID else None

    async def record_notification(self, connector_id: UUID, observed_at: datetime) -> bool:
        self.calls.append("record_notification")
        if self.notification_failure is not None:
            raise self.notification_failure
        if self.state is None or connector_id != self.state.connector_account_id:
            return False
        current = self.state.last_notification_at
        self.state = self.state.model_copy(
            update={
                "last_notification_at": (
                    observed_at if current is None or observed_at > current else current
                )
            }
        )
        return True

    async def claim_sync(
        self, connector_id: UUID, now: datetime, lease_seconds: int
    ) -> SyncClaim | None:
        self.calls.append("claim")
        if self.claim_failure is not None:
            raise self.claim_failure
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
        if self.completion_failure is not None:
            raise self.completion_failure
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

    async def complete_recovery(
        self,
        claim: SyncClaim,
        watch: WatchResult,
        now: datetime,
        next_renewal_at: datetime,
        next_safety_sync_at: datetime,
    ) -> bool:
        self.calls.append("complete_recovery")
        if self.completion_failure is not None:
            raise self.completion_failure
        if self.complete_failures:
            self.complete_failures -= 1
            return False
        assert claim == self.claim and self.state is not None
        self.state = self.state.model_copy(
            update={
                "history_id": watch.history_id,
                "watch_expiration": watch.expiration,
                "last_successful_sync_at": now,
                "next_watch_renewal_at": next_renewal_at,
                "next_safety_sync_at": next_safety_sync_at,
                "claim_id": None,
                "lease_expires_at": None,
            }
        )
        self.busy = False
        return True

    async def release_sync(self, claim: SyncClaim) -> bool:
        self.calls.append("release")
        if self.release_failure is not None:
            raise self.release_failure
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
        self.failure: BaseException | None = None
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
        self.history_failure: BaseException | None = None
        self.message_failure: BaseException | None = None
        self.message_list_failure: BaseException | None = None
        self.watch_failure: BaseException | None = None
        self.history_calls: list[tuple[str, str | None]] = []
        self.message_calls: list[str] = []
        self.message_list_pages: dict[str | None, MessageListPage] = {}
        self.message_list_calls: list[tuple[str, str | None]] = []
        self.watch_result = WatchResult("200", NOW + timedelta(days=7))
        self.watch_calls: list[str] = []
        self.on_watch: Callable[[], Awaitable[None]] | None = None
        self.operations: list[str] = []
        self.close_calls = 0
        self.close_failures: list[BaseException] = []

    async def list_history(self, start_history_id: str, page_token: str | None) -> HistoryPage:
        self.history_calls.append((start_history_id, page_token))
        if self.history_failure is not None:
            raise self.history_failure
        return self.pages[page_token]

    async def get_message(self, message_id: str) -> Mapping[str, object]:
        self.message_calls.append(message_id)
        self.operations.append(f"message:{message_id}")
        if self.message_failure is not None:
            raise self.message_failure
        return self.messages[message_id]

    async def get_profile(self) -> GmailProfile:
        raise AssertionError("synchronization must not load the Gmail profile")

    async def watch(self, topic_name: str) -> WatchResult:
        self.watch_calls.append(topic_name)
        self.operations.append("watch")
        if self.on_watch is not None:
            await self.on_watch()
        if self.watch_failure is not None:
            raise self.watch_failure
        return self.watch_result

    async def list_message_ids(self, query: str, page_token: str | None) -> MessageListPage:
        self.message_list_calls.append((query, page_token))
        if self.message_list_failure is not None:
            raise self.message_list_failure
        return self.message_list_pages[page_token]

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_failures:
            raise self.close_failures.pop(0)


class FakeGmailClientFactory:
    def __init__(self, gmail: FakeGmailClient) -> None:
        self.gmail = gmail
        self.failure: BaseException | None = None
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
        self.failure: BaseException | None = None
        self.operations: list[str] | None = None

    async def ingest(self, command: NewEvent) -> IngestResult:
        self.events.append(command)
        if self.operations is not None:
            self.operations.append(f"event:{command.external_id}")
        if self.failure is not None:
            raise self.failure
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
        self.events.operations = self.gmail.operations
        self.recovery = GmailRecoveryService(
            repository=cast(ConnectorRepository, self.repository),
            event_service=cast(EventService, self.events),
            topic_name="projects/eva/topics/gmail",
        )
        self.service = GmailSyncService(
            repository=cast(ConnectorRepository, self.repository),
            credential_store=cast(CredentialStore, self.credentials),
            client_factory=cast(GmailClientFactory, self.factory),
            event_service=cast(EventService, self.events),
            clock=lambda: NOW,
            lease_seconds=300,
            recovery_service=self.recovery,
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
    assert harness.repository.state.last_notification_at == NOW
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


async def test_sync_uses_injected_safety_interval_for_next_due_time() -> None:
    """Fails if the typed safety setting cannot control ordinary sync scheduling."""
    harness = Harness()
    harness.service = GmailSyncService(
        repository=cast(ConnectorRepository, harness.repository),
        credential_store=cast(CredentialStore, harness.credentials),
        client_factory=cast(GmailClientFactory, harness.factory),
        event_service=cast(EventService, harness.events),
        clock=lambda: NOW,
        lease_seconds=300,
        recovery_service=harness.recovery,
        safety_sync_interval=timedelta(minutes=17),
    )
    harness.gmail.pages = {
        None: HistoryPage(message_ids=(), history_id="108", next_page_token=None)
    }

    await harness.handle(history_id="108")

    assert harness.repository.state is not None
    assert harness.repository.state.next_safety_sync_at == NOW + timedelta(minutes=17)


async def test_already_covered_notification_uses_persisted_cursor_without_provider_work() -> None:
    """Fails if an old wake hint can move the cursor backward or start provider work."""
    harness = Harness(state=sync_record("500"))

    result = await harness.handle(history_id="499")

    assert result == SyncResult(SyncStatus.ALREADY_COVERED, CONNECTOR_ID, 0, "500")
    assert harness.repository.calls == ["find", "record_notification", "get_sync_state"]
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
    assert harness.repository.state is not None
    assert harness.repository.state.last_notification_at == NOW


@pytest.mark.parametrize("status", [ConnectorStatus.CONNECTING, ConnectorStatus.ERROR])
async def test_known_retryable_connector_state_returns_connecting(status: ConnectorStatus) -> None:
    """Fails if an initial-watch race or retryable setup error is terminally acknowledged."""
    harness = Harness(record=connector(status))

    result = await harness.handle()

    assert result == SyncResult(SyncStatus.CONNECTING, CONNECTOR_ID, 0, "100")
    assert harness.credentials.references == []
    assert harness.repository.state is not None
    assert harness.repository.state.last_notification_at == NOW


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
    assert harness.repository.state is not None
    if record is not None and record.status != ConnectorStatus.DISABLED:
        assert harness.repository.state.last_notification_at == NOW
    else:
        assert harness.repository.state.last_notification_at is None


async def test_notification_observation_failure_is_chain_free_and_retryable() -> None:
    """Fails if a known notification can be acknowledged without durable observation."""
    harness = Harness()
    harness.repository.notification_failure = RuntimeError(
        "private-database-response owner@example.com"
    )

    with pytest.raises(GmailSyncError) as raised:
        await harness.handle()

    assert str(raised.value) == "Gmail notification observation failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "owner@example.com" not in repr(raised.value)
    assert harness.credentials.references == []


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
    harness.gmail.history_failure = provider_error

    with pytest.raises(GmailProviderError) as raised:
        await harness.handle()

    assert raised.value is provider_error
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert harness.repository.marked_error is None
    assert harness.repository.busy is False
    assert harness.repository.state is not None and harness.repository.state.history_id == "100"


async def test_recovery_failure_preserves_provider_type_and_releases_claim() -> None:
    """Fails if recovery provider errors lose classification or leave the old lease live."""
    harness = Harness()
    expired = HistoryCursorExpired("Gmail history cursor expired")
    harness.gmail.history_failure = expired
    harness.gmail.message_list_failure = expired

    with pytest.raises(HistoryCursorExpired) as raised:
        await harness.handle()

    assert raised.value is expired
    assert harness.repository.busy is False
    assert harness.repository.state is not None and harness.repository.state.history_id == "100"


async def test_expired_history_recovers_exact_connected_range_after_fresh_watch() -> None:
    """Fails on a broad query, incomplete paging/dedupe, coarse filtering, or late watch."""
    connected_at = datetime.fromtimestamp(1788064200, tz=UTC)
    harness = Harness(record=connector().model_copy(update={"connected_at": connected_at}))
    harness.gmail.history_failure = HistoryCursorExpired("Gmail history cursor expired")
    harness.gmail.message_list_pages = {
        None: MessageListPage(("at-boundary", "before", "removed"), "page-2"),
        "page-2": MessageListPage(("at-boundary", "after"), None),
    }
    harness.gmail.messages = {
        "at-boundary": raw_message("at-boundary", connected_at),
        "before": raw_message("before", connected_at - timedelta(milliseconds=1)),
        "removed": raw_message("removed", connected_at, labels=["CATEGORY_UPDATES"]),
        "after": raw_message("after", connected_at + timedelta(milliseconds=1)),
    }
    harness.gmail.watch_result = WatchResult("250", NOW + timedelta(days=7))

    result = await harness.handle()

    assert result == SyncResult(SyncStatus.SYNCED, CONNECTOR_ID, 2, "250")
    assert harness.gmail.message_list_calls == [
        ("in:inbox after:1788064200", None),
        ("in:inbox after:1788064200", "page-2"),
    ]
    assert harness.gmail.message_calls == ["at-boundary", "before", "removed", "after"]
    assert [event.external_id for event in harness.events.events] == ["at-boundary", "after"]
    assert harness.gmail.watch_calls == ["projects/eva/topics/gmail"]
    assert harness.gmail.operations == [
        "watch",
        "message:at-boundary",
        "event:at-boundary",
        "message:before",
        "message:removed",
        "message:after",
        "event:after",
    ]
    assert harness.repository.state is not None
    assert harness.repository.state.history_id == "250"
    assert harness.repository.state.watch_expiration == NOW + timedelta(days=7)
    assert harness.repository.state.next_watch_renewal_at == NOW + timedelta(hours=24)
    assert harness.repository.state.next_safety_sync_at == NOW + timedelta(minutes=60)
    assert harness.gmail.close_calls == 1


async def test_recovery_watch_precedes_scan_so_cutover_arrival_is_ingested_once() -> None:
    """Fails if scan-then-watch lets a covered cutover message disappear permanently."""
    connected_at = datetime.fromtimestamp(1788064200, tz=UTC)
    harness = Harness(record=connector().model_copy(update={"connected_at": connected_at}))
    harness.gmail.history_failure = HistoryCursorExpired("Gmail history cursor expired")
    harness.gmail.message_list_pages = {None: MessageListPage((), None)}
    harness.gmail.watch_result = WatchResult("250", NOW + timedelta(days=7))

    async def arrive_at_cutover() -> None:
        harness.gmail.message_list_pages = {None: MessageListPage(("cutover",), None)}
        harness.gmail.messages["cutover"] = raw_message(
            "cutover", connected_at + timedelta(milliseconds=1)
        )

    harness.gmail.on_watch = arrive_at_cutover

    first = await harness.handle("250")
    second = await harness.handle("250")

    assert first == SyncResult(SyncStatus.SYNCED, CONNECTOR_ID, 1, "250")
    assert second == SyncResult(SyncStatus.ALREADY_COVERED, CONNECTOR_ID, 0, "250")
    assert [event.external_id for event in harness.events.events] == ["cutover"]
    assert len(harness.events.by_key) == 1
    assert harness.gmail.operations[:3] == ["watch", "message:cutover", "event:cutover"]


async def test_repeated_sync_attempts_close_each_attempt_client() -> None:
    """Fails if a healthy pull loop accumulates open per-attempt Gmail clients."""
    harness = Harness()
    harness.gmail.pages = {
        None: HistoryPage(message_ids=(), history_id="101", next_page_token=None)
    }

    first = await harness.service.sync_connector(CONNECTOR_ID)
    harness.gmail.pages = {
        None: HistoryPage(message_ids=(), history_id="102", next_page_token=None)
    }
    second = await harness.service.sync_connector(CONNECTOR_ID)

    assert first.final_history_id == "101"
    assert second.final_history_id == "102"
    assert harness.gmail.history_calls == [("100", None), ("101", None)]
    assert harness.gmail.close_calls == 2
    assert len(harness.factory.credentials) == 2


async def test_sync_cleanup_cancellation_propagates_identically_after_success() -> None:
    harness = Harness()
    cancellation = asyncio.CancelledError("private-close-marker")
    harness.gmail.close_failures = [cancellation]
    harness.gmail.pages = {
        None: HistoryPage(message_ids=(), history_id="101", next_page_token=None)
    }

    with pytest.raises(asyncio.CancelledError) as raised:
        await harness.service.sync_connector(CONNECTOR_ID)

    assert raised.value is cancellation
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert harness.gmail.close_calls == 1
    assert harness.repository.state is not None
    assert harness.repository.state.history_id == "101"
    assert harness.repository.busy is False


@pytest.mark.parametrize(
    "cleanup_failure",
    [RuntimeError("private-close-response"), asyncio.CancelledError("private-close-cancel")],
    ids=["ordinary", "cancellation"],
)
async def test_sync_primary_failure_wins_over_client_cleanup(
    cleanup_failure: BaseException,
) -> None:
    harness = Harness()
    primary = GmailProviderError("Gmail API request failed")
    harness.gmail.history_failure = primary
    harness.gmail.close_failures = [cleanup_failure]

    with pytest.raises(GmailProviderError) as raised:
        await harness.service.sync_connector(CONNECTOR_ID)

    assert raised.value is primary
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert harness.gmail.close_calls == 1
    assert harness.repository.busy is False


async def test_recovery_replay_is_idempotent_and_cursor_completion_remains_last() -> None:
    """Fails if a recovery crash duplicates Events or publishes the fresh cursor too early."""
    connected_at = datetime.fromtimestamp(1788064200, tz=UTC)
    harness = Harness(record=connector().model_copy(update={"connected_at": connected_at}))
    harness.gmail.history_failure = HistoryCursorExpired("Gmail history cursor expired")
    harness.gmail.message_list_pages = {None: MessageListPage(("message-1",), None)}
    harness.gmail.messages = {"message-1": raw_message("message-1", connected_at)}
    harness.gmail.watch_result = WatchResult("250", NOW + timedelta(days=7))
    harness.repository.complete_failures = 1

    with pytest.raises(GmailSyncError, match="claim is no longer current"):
        await harness.handle()

    assert harness.repository.state is not None
    assert harness.repository.state.history_id == "100"
    assert len(harness.events.by_key) == 1

    replay = await harness.handle()

    assert replay == SyncResult(SyncStatus.SYNCED, CONNECTOR_ID, 0, "250")
    assert len(harness.events.events) == 2
    assert len(harness.events.by_key) == 1
    assert harness.repository.state is not None
    assert harness.repository.state.history_id == "250"


@pytest.mark.parametrize("failure_boundary", ["event", "watch"])
async def test_recovery_failure_keeps_old_cursor_retryable(failure_boundary: str) -> None:
    """Fails if partial recovery work can replace the durable cursor."""
    connected_at = datetime.fromtimestamp(1788064200, tz=UTC)
    harness = Harness(record=connector().model_copy(update={"connected_at": connected_at}))
    harness.gmail.history_failure = HistoryCursorExpired("Gmail history cursor expired")
    harness.gmail.message_list_pages = {None: MessageListPage(("message-1",), None)}
    harness.gmail.messages = {"message-1": raw_message("message-1", connected_at)}
    provider_failure = GmailProviderError("Gmail API request failed")
    if failure_boundary == "event":
        harness.events.failure = RuntimeError("private-message-content")
    else:
        harness.gmail.watch_failure = provider_failure

    expected = GmailSyncError if failure_boundary == "event" else GmailProviderError
    with pytest.raises(expected):
        await harness.handle()

    assert harness.repository.state is not None
    assert harness.repository.state.history_id == "100"
    assert harness.repository.busy is False
    assert harness.gmail.watch_calls == ["projects/eva/topics/gmail"]


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


async def test_invalid_message_with_parser_cause_is_rebuilt_chain_free() -> None:
    """Fails if a normalization parser exception remains reachable through the public chain."""
    harness = Harness()
    harness.gmail.pages = {
        None: HistoryPage(message_ids=("broken",), history_id="101", next_page_token=None)
    }
    harness.gmail.messages = {
        "broken": {
            **raw_message("broken", NOW),
            "internalDate": "999999999999999999999999999999999999999999999999",
        }
    }

    with pytest.raises(ValueError) as raised:
        await harness.handle()

    assert str(raised.value) == "invalid Gmail message"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert harness.repository.busy is False


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


@pytest.mark.parametrize(
    "stage",
    ["credentials", "factory", "history", "message", "event", "completion"],
)
async def test_cancellation_during_claimed_await_releases_claim_and_propagates_unchanged(
    stage: str,
) -> None:
    """Fails if worker shutdown strands a lease or cancellation changes classification."""
    harness = Harness()
    cancellation = asyncio.CancelledError(f"cancelled-at-{stage}")
    harness.gmail.pages = {
        None: HistoryPage(message_ids=("message-1",), history_id="101", next_page_token=None)
    }
    harness.gmail.messages = {"message-1": raw_message("message-1", NOW)}
    if stage == "credentials":
        harness.credentials.failure = cancellation
    elif stage == "factory":
        harness.factory.failure = cancellation
    elif stage == "history":
        harness.gmail.history_failure = cancellation
    elif stage == "message":
        harness.gmail.message_failure = cancellation
    elif stage == "event":
        harness.events.failure = cancellation
    else:
        harness.repository.completion_failure = cancellation

    with pytest.raises(asyncio.CancelledError) as raised:
        await harness.handle()

    assert raised.value is cancellation
    assert harness.repository.busy is False
    assert harness.repository.calls[-1] == "release"
    assert harness.repository.state is not None
    assert harness.repository.state.history_id == "100"
    assert harness.gmail.close_calls == int(stage not in {"credentials", "factory"})


async def test_cancellation_during_reauthorization_transition_releases_claim() -> None:
    """Fails if cancellation during the terminal transition is masked or strands its lease."""
    harness = Harness()
    harness.credentials.failure = AuthorizationRevoked("Google authorization has been revoked")
    cancellation = asyncio.CancelledError("cancelled-during-transition")
    harness.repository.transition_failure = cancellation

    with pytest.raises(asyncio.CancelledError) as raised:
        await harness.handle()

    assert raised.value is cancellation
    assert harness.repository.busy is False
    assert harness.repository.calls[-1] == "release"
    assert harness.repository.state is not None
    assert harness.repository.state.history_id == "100"


@pytest.mark.parametrize(
    "boundary",
    ["identity_lookup", "handle_sync_read", "connector_read", "sync_read", "claim"],
)
async def test_repository_failures_are_content_free_retryable_errors(boundary: str) -> None:
    """Fails if SQL parameters or mailbox identity escape a repository boundary."""
    harness = Harness()
    marker = f"private-{boundary}@example.com"
    failure = content_bearing_error(marker)
    if boundary == "identity_lookup":
        harness.repository.find_failure = failure
        operation = harness.handle()
    elif boundary == "handle_sync_read":
        harness.repository.get_sync_failure = failure
        operation = harness.handle()
    elif boundary == "connector_read":
        harness.repository.get_failure = failure
        operation = harness.service.sync_connector(CONNECTOR_ID)
    elif boundary == "sync_read":
        harness.repository.get_sync_failure = failure
        operation = harness.service.sync_connector(CONNECTOR_ID)
    else:
        harness.repository.claim_failure = failure
        operation = harness.service.sync_connector(CONNECTOR_ID)

    with pytest.raises(GmailSyncError) as raised:
        await operation

    assert_content_free_sync_error(raised.value, marker)
    assert harness.repository.busy is False


@pytest.mark.parametrize("boundary", ["event", "completion"])
async def test_claimed_database_failures_release_and_remove_message_content(
    boundary: str,
) -> None:
    """Fails if Event or cursor SQL errors retain normalized email content or a live claim."""
    harness = Harness()
    marker = f"private-{boundary}-subject-and-body"
    failure = content_bearing_error(marker)
    harness.gmail.pages = {
        None: HistoryPage(message_ids=("message-1",), history_id="101", next_page_token=None)
    }
    harness.gmail.messages = {"message-1": raw_message("message-1", NOW)}
    if boundary == "event":
        harness.events.failure = failure
    else:
        harness.repository.completion_failure = failure

    with pytest.raises(GmailSyncError) as raised:
        await harness.handle()

    assert_content_free_sync_error(raised.value, marker)
    assert harness.repository.busy is False
    assert harness.repository.calls[-1] == "release"
    assert harness.repository.state is not None
    assert harness.repository.state.history_id == "100"


@pytest.mark.parametrize(
    "failure",
    [
        GmailProviderError("Gmail API request failed"),
        SecretManagerProviderError("Secret Manager credential read failed"),
        HistoryCursorExpired("Gmail history cursor expired"),
        GmailSyncError("Gmail synchronization claim is no longer current"),
    ],
)
async def test_safe_retryable_exception_types_remain_unchanged(failure: Exception) -> None:
    """Fails if later workers lose the provider, recovery, or synchronization classification."""
    harness = Harness()
    harness.credentials.failure = failure

    with pytest.raises(type(failure)) as raised:
        await harness.handle()

    assert raised.value is failure
    assert harness.repository.busy is False


async def test_cancellation_is_not_masked_when_claim_release_also_fails() -> None:
    """Fails if an independent cleanup error replaces worker cancellation."""
    harness = Harness()
    cancellation = asyncio.CancelledError("cancelled-provider-operation")
    harness.gmail.history_failure = cancellation
    harness.repository.release_failure = RuntimeError("private-release-response")

    with pytest.raises(asyncio.CancelledError) as raised:
        await harness.handle()

    assert raised.value is cancellation
    assert harness.repository.calls[-1] == "release"
