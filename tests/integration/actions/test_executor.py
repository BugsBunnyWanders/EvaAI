from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import select, update

from eva_ai.actions.executor import ActionCredentialStore, ActionExecutor, ActionExecutorStore
from eva_ai.actions.repository import ActionRepository
from eva_ai.actions.types import (
    ActionExecutionOutcome,
    ActionStatus,
    ActionTaskRequest,
)
from eva_ai.connectors.gmail.contracts import (
    AuthorizationRevoked,
    GmailActionClient,
    GmailActionClientFactory,
    GmailDraftResult,
    GmailSendResult,
)
from eva_ai.connectors.repository import ConnectorRepository
from eva_ai.connectors.types import ConnectorStatus
from eva_ai.db import Database
from eva_ai.db.models import (
    Action,
    ActionApproval,
    ConnectorAccount,
    GmailSyncState,
    ManagedGmailDraft,
    Notification,
    OutboxMessage,
)
from eva_ai.integrations.gmail.api import GmailActionReauthorizationRequired
from eva_ai.integrations.gmail.oauth import GMAIL_COMPOSE_SCOPE, GMAIL_READONLY_SCOPE
from tests.integration.actions.test_repository import _new_create_proposal, _seed_action_scope


class Credentials:
    async def get(self, secret_reference: str) -> str:
        assert secret_reference.startswith("secret-")
        return "authorized-user-json"


class Client:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.create_calls = 0

    async def create_draft(self, raw: str, thread_id: str | None) -> GmailDraftResult:
        assert raw
        self.create_calls += 1
        if self.error is not None:
            raise self.error
        return GmailDraftResult("provider-draft", "provider-message", thread_id)

    async def update_draft(
        self, draft_id: str, raw: str, thread_id: str | None
    ) -> GmailDraftResult:
        raise AssertionError("update was not expected")

    async def delete_draft(self, draft_id: str) -> None:
        raise AssertionError("delete was not expected")

    async def send_draft(self, draft_id: str) -> GmailSendResult:
        raise AssertionError("send was not expected")

    async def get_draft(self, draft_id: str) -> Mapping[str, object]:
        raise AssertionError("draft read was not expected")

    async def close(self) -> None:
        return None


class Factory:
    def __init__(self, client: Client) -> None:
        self.client = client

    async def create_action(self, authorized_user_json: str) -> GmailActionClient:
        assert authorized_user_json == "authorized-user-json"
        return cast(GmailActionClient, self.client)


async def _create_executor_action(
    database: Database,
    *,
    now: datetime,
    notification_destination: str | None = None,
) -> tuple[ActionRepository, ConnectorAccount, Action]:
    scope, connector_id, event_id, _, _ = await _seed_action_scope(database)
    async with database.session() as session:
        async with session.begin():
            connector = await session.get(ConnectorAccount, connector_id)
            assert connector is not None
            connector.granted_scopes = [GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE]
    repository = ActionRepository(
        database,
        notification_destination=notification_destination,
    )
    created = await repository.create_allowed_action(
        proposal=_new_create_proposal(
            scope,
            connector_id,
            event_id,
            now,
            source_key=f"executor:{connector_id}",
        ),
        destination="eva-actions",
    )
    async with database.session() as session:
        connector = await session.get(ConnectorAccount, connector_id)
        action = await session.get(Action, created.action.id)
        assert connector is not None and action is not None
        session.expunge(connector)
        session.expunge(action)
    return repository, connector, action


def _executor(
    repository: ActionRepository,
    client: Client,
    *,
    now: datetime,
) -> ActionExecutor:
    return ActionExecutor(
        cast(ActionExecutorStore, repository),
        cast(ActionCredentialStore, Credentials()),
        cast(GmailActionClientFactory, Factory(client)),
        lease_seconds=60,
        approval_ttl=timedelta(hours=24),
        clock=lambda: now,
    )


@pytest.mark.integration
async def test_create_execution_is_atomic_and_task_replay_is_terminal(database: Database) -> None:
    now = datetime.now(UTC)
    repository, _, action = await _create_executor_action(database, now=now)
    client = Client()
    executor = _executor(repository, client, now=now)

    first = await executor.execute(ActionTaskRequest(action_id=action.id))
    replay = await executor.execute(ActionTaskRequest(action_id=action.id))

    assert first.outcome is ActionExecutionOutcome.SUCCEEDED
    assert replay.outcome is ActionExecutionOutcome.TERMINAL
    assert client.create_calls == 1
    async with database.session() as session:
        stored_action = await session.get(Action, action.id)
        managed = await session.scalar(
            select(ManagedGmailDraft).where(
                ManagedGmailDraft.create_proposal_id == action.proposal_id
            )
        )
        assert managed is not None
        approval = await session.scalar(
            select(ActionApproval).where(
                ActionApproval.proposal_id == managed.active_send_proposal_id
            )
        )
    assert stored_action is not None and stored_action.status == ActionStatus.SUCCEEDED
    assert managed is not None and managed.provider_draft_id == "provider-draft"
    assert approval is not None


@pytest.mark.integration
async def test_action_authorization_failure_preserves_read_ingestion(database: Database) -> None:
    now = datetime.now(UTC)
    repository, connector, action = await _create_executor_action(
        database,
        now=now,
        notification_destination="eva-telegram-delivery",
    )
    async with database.session() as session:
        async with session.begin():
            session.add(
                GmailSyncState(
                    connector_account_id=connector.id,
                    history_id="history-42",
                )
            )
    executor = _executor(
        repository,
        Client(error=GmailActionReauthorizationRequired("compose scope missing")),
        now=now,
    )

    result = await executor.execute(ActionTaskRequest(action_id=action.id))

    assert result.outcome is ActionExecutionOutcome.UNAVAILABLE
    async with database.session() as session:
        stored_connector = await session.get(ConnectorAccount, connector.id)
        sync = await session.get(GmailSyncState, connector.id)
        notification = await session.scalar(
            select(Notification).where(Notification.dedupe_key == f"action:{action.id}:failed")
        )
        delivery = (
            None
            if notification is None
            else await session.scalar(
                select(OutboxMessage).where(
                    OutboxMessage.payload["notification_id"].astext == str(notification.id)
                )
            )
        )
    assert stored_connector is not None
    assert stored_connector.status == ConnectorStatus.ACTIVE
    assert stored_connector.last_error_type == "ActionAuthorizationUnavailable"
    assert stored_connector.secret_reference == connector.secret_reference
    assert sync is not None and sync.history_id == "history-42"
    assert notification is not None and "Mail reading remains active" in notification.message
    assert delivery is not None
    assert await ConnectorRepository(database).claim_sync(connector.id, now, 60) is not None


@pytest.mark.integration
async def test_revoked_grant_requires_full_connector_reauthorization(database: Database) -> None:
    now = datetime.now(UTC)
    repository, connector, action = await _create_executor_action(database, now=now)
    executor = _executor(
        repository,
        Client(error=AuthorizationRevoked("revoked")),
        now=now,
    )

    result = await executor.execute(ActionTaskRequest(action_id=action.id))

    assert result.outcome is ActionExecutionOutcome.UNAVAILABLE
    async with database.session() as session:
        stored = await session.get(ConnectorAccount, connector.id)
    assert stored is not None
    assert stored.status == ConnectorStatus.REAUTHORIZATION_REQUIRED


@pytest.mark.integration
async def test_expired_post_boundary_claim_becomes_unknown_without_repeating_provider_call(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    repository, _, action = await _create_executor_action(database, now=now)
    claimed = await repository.claim_action(
        ActionTaskRequest(action_id=action.id),
        now=now,
        lease_seconds=1,
    )
    assert claimed.claim is not None
    assert await repository.mark_provider_call_started(claimed.claim, started_at=now)
    async with database.session() as session:
        async with session.begin():
            await session.execute(
                update(Action)
                .where(Action.id == action.id)
                .values(lease_expires_at=now - timedelta(seconds=1))
            )
    client = Client()
    executor = _executor(repository, client, now=now)

    result = await executor.execute(ActionTaskRequest(action_id=action.id))

    assert result.outcome is ActionExecutionOutcome.UNKNOWN
    assert client.create_calls == 0
