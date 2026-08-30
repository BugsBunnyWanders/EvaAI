from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from eva_ai.connectors.gmail.bootstrap import (
    AccountIdentityMismatch,
    ConnectGmail,
    GmailBootstrapService,
)
from eva_ai.connectors.gmail.contracts import (
    AuthorizedUserGrant,
    CredentialStore,
    GmailClient,
    GmailClientFactory,
    OAuthAuthorizer,
    WatchResult,
)
from eva_ai.connectors.repository import ConnectorRepository
from eva_ai.connectors.types import ConnectorRecord, ConnectorStatus
from eva_ai.integrations.gcp.secret_manager import SecretManagerProviderError
from eva_ai.integrations.gmail.api import GmailProviderError
from eva_ai.integrations.gmail.oauth import GMAIL_READONLY_SCOPE

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)
LATER = NOW + timedelta(days=3)
USER_ID = UUID("0191cafe-7b00-7000-8000-000000000001")
WORKSPACE_ID = UUID("0191cafe-7b00-7000-8000-000000000002")
CONNECTOR_ID = UUID("0191cafe-7b00-7000-8000-000000000003")
CLIENT_FILE = Path("oauth-client.json")
TOPIC_NAME = "projects/eva/topics/eva-gmail-notifications"
AUTHORIZED_USER_JSON = '{"type":"authorized_user","refresh_token":"synthetic"}'
SECRET_REFERENCE = "projects/eva/secrets/eva-gmail-oauth-connector"
WATCH = WatchResult(history_id="812", expiration=NOW + timedelta(days=7))


class FakeRepository:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.record: ConnectorRecord | None = None
        self.history_id: str | None = None
        self.last_error_type: str | None = None
        self.reserve_arguments: tuple[UUID, UUID, str, tuple[str, ...], datetime] | None = None
        self.activation_arguments: (
            tuple[
                UUID,
                WatchResult,
                datetime,
                datetime,
                datetime,
            ]
            | None
        ) = None

    async def reserve_gmail(
        self,
        user_id: UUID,
        workspace_id: UUID,
        account_identity: str,
        granted_scopes: tuple[str, ...],
        now: datetime,
    ) -> ConnectorRecord:
        self.calls.append("reserve")
        self.reserve_arguments = (
            user_id,
            workspace_id,
            account_identity,
            granted_scopes,
            now,
        )
        if self.record is None:
            self.record = ConnectorRecord(
                id=CONNECTOR_ID,
                user_id=user_id,
                workspace_id=workspace_id,
                provider="gmail",
                account_identity=account_identity.lower(),
                granted_scopes=granted_scopes,
                status=ConnectorStatus.CONNECTING,
                secret_reference=None,
                connected_at=None,
            )
        return self.record

    async def attach_secret(self, connector_id: UUID, secret_reference: str) -> ConnectorRecord:
        self.calls.append("attach_secret")
        assert self.record is not None and connector_id == self.record.id
        self.record = self.record.model_copy(update={"secret_reference": secret_reference})
        return self.record

    async def activate_initial_watch(
        self,
        connector_id: UUID,
        watch: WatchResult,
        now: datetime,
        next_renewal_at: datetime,
        next_safety_sync_at: datetime,
    ) -> ConnectorRecord:
        self.calls.append("activate")
        assert self.record is not None and connector_id == self.record.id
        self.activation_arguments = (
            connector_id,
            watch,
            now,
            next_renewal_at,
            next_safety_sync_at,
        )
        self.history_id = watch.history_id
        self.record = self.record.model_copy(
            update={
                "status": ConnectorStatus.ACTIVE,
                "connected_at": self.record.connected_at or now,
            }
        )
        return self.record

    async def mark_reauthorization_required(self, connector_id: UUID, error: BaseException) -> None:
        self.calls.append("mark_reauthorization_required")
        assert self.record is not None and connector_id == self.record.id
        self.last_error_type = type(error).__name__
        self.record = self.record.model_copy(
            update={"status": ConnectorStatus.REAUTHORIZATION_REQUIRED}
        )

    def observe_initial_notification(self) -> None:
        self.calls.append("notification")
        assert self.record is not None
        assert self.record.status == ConnectorStatus.CONNECTING
        assert self.history_id is None


class FakeAuthorizer:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.arguments: tuple[Path, tuple[str, ...]] | None = None

    async def authorize(self, client_file: Path, scopes: tuple[str, ...]) -> AuthorizedUserGrant:
        self.calls.append("authorize")
        self.arguments = (client_file, scopes)
        return AuthorizedUserGrant(authorized_user_json=AUTHORIZED_USER_JSON)


class FakeCredentialStore:
    def __init__(
        self,
        calls: list[str],
        failure: SecretManagerProviderError | None = None,
    ) -> None:
        self.calls = calls
        self.failure = failure
        self.put_arguments: tuple[UUID, str] | None = None

    async def put(self, connector_id: UUID, authorized_user_json: str) -> str:
        self.calls.append("put_secret")
        self.put_arguments = (connector_id, authorized_user_json)
        if self.failure is not None:
            raise self.failure
        return SECRET_REFERENCE

    async def get(self, secret_reference: str) -> str:
        raise AssertionError("bootstrap must not load stored credentials")


class FakeGmailClient:
    def __init__(
        self,
        calls: list[str],
        *,
        identity: str = "Owner@Example.COM",
        watch_failure: GmailProviderError | None = None,
        on_watch: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.calls = calls
        self.identity = identity
        self.watch_failure = watch_failure
        self.on_watch = on_watch
        self.watch_topic: str | None = None

    async def get_profile(self) -> str:
        self.calls.append("get_profile")
        return self.identity

    async def watch(self, topic_name: str) -> WatchResult:
        self.calls.append("watch")
        self.watch_topic = topic_name
        if self.on_watch is not None:
            await self.on_watch()
        if self.watch_failure is not None:
            raise self.watch_failure
        return WATCH

    async def list_history(self, start_history_id: str, page_token: str | None) -> object:
        raise AssertionError("bootstrap must not list history")

    async def get_message(self, message_id: str) -> object:
        raise AssertionError("bootstrap must not get messages")

    async def list_message_ids(self, query: str, page_token: str | None) -> object:
        raise AssertionError("bootstrap must not list messages")


class FakeGmailClientFactory:
    def __init__(self, calls: list[str], gmail: FakeGmailClient) -> None:
        self.calls = calls
        self.gmail = gmail
        self.authorized_user_json: str | None = None

    async def create(self, authorized_user_json: str) -> GmailClient:
        self.calls.append("create_client")
        self.authorized_user_json = authorized_user_json
        return cast(GmailClient, self.gmail)


class Harness:
    def __init__(
        self,
        *,
        identity: str = "Owner@Example.COM",
        secret_failure: SecretManagerProviderError | None = None,
        watch_failure: GmailProviderError | None = None,
        on_watch: Callable[[], Awaitable[None]] | None = None,
        now: datetime = NOW,
    ) -> None:
        self.calls: list[str] = []
        self.repository = FakeRepository(self.calls)
        self.authorizer = FakeAuthorizer(self.calls)
        self.credential_store = FakeCredentialStore(self.calls, secret_failure)
        self.gmail = FakeGmailClient(
            self.calls,
            identity=identity,
            watch_failure=watch_failure,
            on_watch=on_watch,
        )
        self.client_factory = FakeGmailClientFactory(self.calls, self.gmail)
        self.service = GmailBootstrapService(
            repository=cast(ConnectorRepository, self.repository),
            authorizer=cast(OAuthAuthorizer, self.authorizer),
            credential_store=cast(CredentialStore, self.credential_store),
            client_factory=cast(GmailClientFactory, self.client_factory),
            clock=lambda: now,
        )

    async def connect(self) -> ConnectorRecord:
        return await self.service.connect(
            ConnectGmail(
                user_id=USER_ID,
                workspace_id=WORKSPACE_ID,
                expected_identity="owner@example.com",
                client_file=CLIENT_FILE,
                topic_name=TOPIC_NAME,
            )
        )


async def test_connect_runs_exact_bootstrap_sequence_and_activates_from_watch_cursor() -> None:
    """Fails on reordering, wrong boundary arguments, or cursor persistence before watch."""
    harness = Harness()

    connector = await harness.connect()

    assert harness.calls == [
        "authorize",
        "create_client",
        "get_profile",
        "reserve",
        "put_secret",
        "attach_secret",
        "watch",
        "activate",
    ]
    assert harness.authorizer.arguments == (CLIENT_FILE, (GMAIL_READONLY_SCOPE,))
    assert harness.client_factory.authorized_user_json == AUTHORIZED_USER_JSON
    assert harness.repository.reserve_arguments == (
        USER_ID,
        WORKSPACE_ID,
        "owner@example.com",
        (GMAIL_READONLY_SCOPE,),
        NOW,
    )
    assert harness.credential_store.put_arguments == (CONNECTOR_ID, AUTHORIZED_USER_JSON)
    assert harness.gmail.watch_topic == TOPIC_NAME
    assert harness.repository.activation_arguments == (
        CONNECTOR_ID,
        WATCH,
        NOW,
        NOW + timedelta(hours=24),
        NOW + timedelta(minutes=60),
    )
    assert connector.status == ConnectorStatus.ACTIVE
    assert connector.connected_at == NOW
    assert harness.repository.history_id == WATCH.history_id


async def test_connect_rejects_oauth_identity_mismatch_before_reserving_or_storing() -> None:
    """Fails if OAuth identity can select ownership or mismatches leave persisted state."""
    harness = Harness(identity="unexpected@example.com")

    with pytest.raises(
        AccountIdentityMismatch,
        match="^authorized Gmail account does not match configuration$",
    ) as raised:
        await harness.connect()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert harness.calls == ["authorize", "create_client", "get_profile"]
    assert harness.repository.record is None
    assert harness.credential_store.put_arguments is None


async def test_connect_marks_reserved_connector_when_secret_storage_fails() -> None:
    """Fails if a secret-store failure leaves CONNECTING or persists unsafe error text."""
    harness = Harness(
        secret_failure=SecretManagerProviderError("Secret Manager credential write failed")
    )

    with pytest.raises(SecretManagerProviderError, match="credential write failed"):
        await harness.connect()

    assert harness.calls == [
        "authorize",
        "create_client",
        "get_profile",
        "reserve",
        "put_secret",
        "mark_reauthorization_required",
    ]
    assert harness.repository.record is not None
    assert harness.repository.record.status == ConnectorStatus.REAUTHORIZATION_REQUIRED
    assert harness.repository.last_error_type == "SecretManagerProviderError"
    assert harness.repository.record.secret_reference is None
    assert harness.repository.history_id is None


async def test_connect_marks_reserved_connector_and_keeps_cursor_empty_when_watch_fails() -> None:
    """Fails if a watch failure leaves CONNECTING/ACTIVE or stores an unconfirmed cursor."""
    harness = Harness(watch_failure=GmailProviderError("Gmail API request failed"))

    with pytest.raises(GmailProviderError, match="Gmail API request failed"):
        await harness.connect()

    assert harness.calls == [
        "authorize",
        "create_client",
        "get_profile",
        "reserve",
        "put_secret",
        "attach_secret",
        "watch",
        "mark_reauthorization_required",
    ]
    assert harness.repository.record is not None
    assert harness.repository.record.status == ConnectorStatus.REAUTHORIZATION_REQUIRED
    assert harness.repository.last_error_type == "GmailProviderError"
    assert harness.repository.record.secret_reference == SECRET_REFERENCE
    assert harness.repository.history_id is None


async def test_reconnect_reuses_connector_and_preserves_original_connected_at() -> None:
    """Fails if reauthorization creates a connector or moves the no-backfill boundary."""
    harness = Harness()
    first = await harness.connect()
    assert first.connected_at == NOW
    await harness.repository.mark_reauthorization_required(
        CONNECTOR_ID, GmailProviderError("Gmail authorization refresh failed")
    )
    harness.calls.clear()
    harness.service = GmailBootstrapService(
        repository=cast(ConnectorRepository, harness.repository),
        authorizer=cast(OAuthAuthorizer, harness.authorizer),
        credential_store=cast(CredentialStore, harness.credential_store),
        client_factory=cast(GmailClientFactory, harness.client_factory),
        clock=lambda: LATER,
    )

    reconnected = await harness.connect()

    assert reconnected.id == first.id == CONNECTOR_ID
    assert reconnected.connected_at == first.connected_at == NOW
    assert reconnected.status == ConnectorStatus.ACTIVE
    assert harness.repository.activation_arguments is not None
    assert harness.repository.activation_arguments[2] == LATER
    assert harness.calls == [
        "authorize",
        "create_client",
        "get_profile",
        "reserve",
        "put_secret",
        "attach_secret",
        "watch",
        "activate",
    ]


async def test_initial_watch_notification_observes_connecting_before_activation() -> None:
    """Fails if reservation is delayed until after watch or activation starts too early."""
    harness: Harness

    async def notify_during_watch() -> None:
        harness.repository.observe_initial_notification()

    harness = Harness(on_watch=notify_during_watch)

    connector = await harness.connect()

    assert connector.status == ConnectorStatus.ACTIVE
    assert harness.calls == [
        "authorize",
        "create_client",
        "get_profile",
        "reserve",
        "put_secret",
        "attach_secret",
        "watch",
        "notification",
        "activate",
    ]
