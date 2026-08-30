import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from eva_ai.connectors.repository import (
    AmbiguousConnectorIdentity,
    ConnectorRepository,
    ConnectorScopeMismatchError,
)
from eva_ai.connectors.types import ConnectorStatus
from eva_ai.db import Database
from eva_ai.db.models import ConnectorAccount
from tests.integration.factories import Scope, create_scope, gmail_watch

NOW = datetime(2030, 1, 1, tzinfo=UTC)


class AuthorizationFailure(RuntimeError):
    pass


async def reserve_active(
    repository: ConnectorRepository,
    scope: Scope,
    *,
    history_id: str = "100",
    now: datetime = NOW,
) -> UUID:
    connector = await repository.reserve_gmail(
        scope.user_id,
        scope.workspace_id,
        "Owner@Example.COM",
        ("https://www.googleapis.com/auth/gmail.readonly",),
        now,
    )
    await repository.attach_secret(connector.id, "projects/eva/secrets/gmail/versions/1")
    await repository.activate_initial_watch(
        connector.id,
        gmail_watch(history_id, now + timedelta(days=7)),
        now,
        now + timedelta(days=1),
        now + timedelta(hours=1),
    )
    return connector.id


@pytest.mark.integration
async def test_reserve_gmail_normalizes_identity_and_is_stable_within_workspace(
    database: Database,
) -> None:
    scope = await create_scope(database)
    repository = ConnectorRepository(database)
    identity = f"Owner+{scope.workspace_id}@Example.COM"

    first = await repository.reserve_gmail(
        scope.user_id,
        scope.workspace_id,
        identity,
        ("scope-a",),
        NOW,
    )
    second = await repository.reserve_gmail(
        scope.user_id,
        scope.workspace_id,
        identity.lower(),
        ("scope-b",),
        NOW + timedelta(minutes=1),
    )
    state = await repository.get_sync_state(first.id)
    found = await repository.find_by_identity(identity.upper())
    missing = await repository.find_by_identity("missing@example.com")
    assert first.id == second.id
    assert first.account_identity == identity.lower()
    assert first.status == ConnectorStatus.CONNECTING
    assert first.secret_reference is None and first.connected_at is None
    assert state is not None and state.history_id is None
    assert found == first
    assert missing is None

    other_scope = await create_scope(database)
    other = await repository.reserve_gmail(
        other_scope.user_id,
        other_scope.workspace_id,
        identity.lower(),
        ("scope-a",),
        NOW,
    )

    assert first.id != other.id


@pytest.mark.integration
async def test_reserve_gmail_rejects_conflicting_user_without_returning_secret(
    database: Database,
) -> None:
    repository = ConnectorRepository(database)
    owner = await create_scope(database)
    other_user = await create_scope(database)
    identity = f"owner+{owner.workspace_id}@example.com"
    connector = await repository.reserve_gmail(
        owner.user_id,
        owner.workspace_id,
        identity,
        ("scope",),
        NOW,
    )
    await repository.attach_secret(connector.id, "projects/eva/secrets/gmail/versions/secret")

    with pytest.raises(ConnectorScopeMismatchError, match="persisted owner"):
        await repository.reserve_gmail(
            other_user.user_id,
            owner.workspace_id,
            identity,
            ("scope",),
            NOW,
        )

    stored = await repository.get(connector.id)
    assert stored is not None
    assert stored.user_id == owner.user_id
    assert stored.secret_reference == "projects/eva/secrets/gmail/versions/secret"


@pytest.mark.integration
async def test_find_by_identity_rejects_cross_workspace_ambiguity(database: Database) -> None:
    repository = ConnectorRepository(database)
    first_scope = await create_scope(database)
    second_scope = await create_scope(database)
    identity = f"shared+{first_scope.workspace_id}@example.com"
    await repository.reserve_gmail(
        first_scope.user_id,
        first_scope.workspace_id,
        identity,
        ("scope",),
        NOW,
    )
    await repository.reserve_gmail(
        second_scope.user_id,
        second_scope.workspace_id,
        identity,
        ("scope",),
        NOW,
    )

    with pytest.raises(AmbiguousConnectorIdentity, match="multiple connectors"):
        await repository.find_by_identity(identity.upper())


@pytest.mark.integration
async def test_initial_activation_requires_a_secret_reference(database: Database) -> None:
    scope = await create_scope(database)
    repository = ConnectorRepository(database)
    connector = await repository.reserve_gmail(
        scope.user_id, scope.workspace_id, "owner@example.com", ("scope",), NOW
    )

    with pytest.raises(ValueError, match="secret"):
        await repository.activate_initial_watch(
            connector.id,
            gmail_watch("100", NOW + timedelta(days=7)),
            NOW,
            NOW + timedelta(days=1),
            NOW + timedelta(hours=1),
        )

    stored = await repository.get(connector.id)
    assert stored is not None and stored.status == ConnectorStatus.CONNECTING


@pytest.mark.integration
async def test_initial_activation_preserves_first_connection_boundary_on_reauthorization(
    database: Database,
) -> None:
    scope = await create_scope(database)
    repository = ConnectorRepository(database)
    connector_id = await reserve_active(repository, scope, history_id="100")
    original = await repository.get(connector_id)
    assert original is not None

    await repository.mark_reauthorization_required(
        connector_id, AuthorizationFailure("refresh-token=secret account-content")
    )
    await repository.attach_secret(connector_id, "projects/eva/secrets/gmail/versions/2")
    reactivated = await repository.activate_initial_watch(
        connector_id,
        gmail_watch("200", NOW + timedelta(days=14)),
        NOW + timedelta(days=2),
        NOW + timedelta(days=3),
        NOW + timedelta(days=2, hours=1),
    )
    state = await repository.get_sync_state(connector_id)

    assert reactivated.status == ConnectorStatus.ACTIVE
    assert reactivated.connected_at == original.connected_at == NOW
    assert reactivated.secret_reference == "projects/eva/secrets/gmail/versions/2"
    assert state is not None and state.history_id == "200"

    repeated = await repository.activate_initial_watch(
        connector_id,
        gmail_watch("300", NOW + timedelta(days=21)),
        NOW + timedelta(days=4),
        NOW + timedelta(days=5),
        NOW + timedelta(days=4, hours=1),
    )
    repeated_state = await repository.get_sync_state(connector_id)

    assert repeated.connected_at == NOW
    assert repeated_state is not None and repeated_state.history_id == "200"


@pytest.mark.integration
async def test_concurrent_claimers_receive_only_one_active_lease(database: Database) -> None:
    scope = await create_scope(database)
    repository = ConnectorRepository(database)
    connector_id = await reserve_active(repository, scope)

    first, second = await asyncio.gather(
        repository.claim_sync(connector_id, NOW, lease_seconds=300),
        repository.claim_sync(connector_id, NOW, lease_seconds=300),
    )
    claims = [claim for claim in (first, second) if claim is not None]
    state = await repository.get_sync_state(connector_id)

    assert len(claims) == 1
    assert claims[0].claim_id.version == 7
    assert claims[0].sync.history_id == "100"
    assert claims[0].lease_expires_at == NOW + timedelta(seconds=300)
    assert state is not None and state.claim_id == claims[0].claim_id


@pytest.mark.integration
async def test_expired_claim_is_reclaimed_and_stale_completion_cannot_advance_cursor(
    database: Database,
) -> None:
    scope = await create_scope(database)
    repository = ConnectorRepository(database)
    connector_id = await reserve_active(repository, scope)
    stale_claim = await repository.claim_sync(connector_id, NOW, lease_seconds=30)
    assert stale_claim is not None

    current_claim = await repository.claim_sync(
        connector_id, NOW + timedelta(seconds=31), lease_seconds=300
    )
    assert current_claim is not None
    completed = await repository.complete_sync(
        stale_claim,
        "999",
        NOW + timedelta(seconds=31),
        NOW + timedelta(hours=1),
    )
    state = await repository.get_sync_state(connector_id)

    assert current_claim.claim_id != stale_claim.claim_id
    assert completed is False
    assert state is not None and state.history_id == "100"
    assert state.claim_id == current_claim.claim_id


@pytest.mark.integration
async def test_complete_sync_advances_only_nonblank_nondecreasing_history_cursor(
    database: Database,
) -> None:
    scope = await create_scope(database)
    repository = ConnectorRepository(database)
    connector_id = await reserve_active(repository, scope)
    claim = await repository.claim_sync(connector_id, NOW, lease_seconds=300)
    assert claim is not None

    advanced = await repository.complete_sync(claim, "101", NOW, NOW + timedelta(hours=1))
    next_claim = await repository.claim_sync(connector_id, NOW + timedelta(minutes=1), 300)
    assert next_claim is not None
    lower = await repository.complete_sync(
        next_claim, "100", NOW + timedelta(minutes=1), NOW + timedelta(hours=2)
    )
    blank = await repository.complete_sync(
        next_claim, "", NOW + timedelta(minutes=1), NOW + timedelta(hours=2)
    )
    state = await repository.get_sync_state(connector_id)

    assert advanced is True
    assert lower is False and blank is False
    assert state is not None and state.history_id == "101"
    assert state.claim_id == next_claim.claim_id


@pytest.mark.integration
async def test_release_sync_only_releases_current_claim(database: Database) -> None:
    scope = await create_scope(database)
    repository = ConnectorRepository(database)
    connector_id = await reserve_active(repository, scope)
    stale_claim = await repository.claim_sync(connector_id, NOW, lease_seconds=1)
    assert stale_claim is not None
    current_claim = await repository.claim_sync(connector_id, NOW + timedelta(seconds=2), 300)
    assert current_claim is not None

    stale_released = await repository.release_sync(stale_claim)
    current_released = await repository.release_sync(current_claim)
    state = await repository.get_sync_state(connector_id)

    assert stale_released is False and current_released is True
    assert state is not None and state.claim_id is None and state.lease_expires_at is None


@pytest.mark.integration
async def test_due_maintenance_loads_only_active_connectors_with_due_work(
    database: Database,
) -> None:
    scope = await create_scope(database)
    repository = ConnectorRepository(database)
    due_id = await reserve_active(repository, scope)
    future_scope = await create_scope(database)
    future_id = await reserve_active(repository, future_scope)
    future_claim = await repository.claim_sync(future_id, NOW, 300)
    assert future_claim is not None
    await repository.record_watch_renewal(
        future_claim,
        NOW + timedelta(days=10),
        NOW + timedelta(days=5),
    )
    safety_claim = await repository.claim_sync(future_id, NOW, 300)
    assert safety_claim is not None
    assert await repository.complete_sync(
        safety_claim,
        "100",
        NOW,
        NOW + timedelta(days=5),
    )
    await repository.mark_reauthorization_required(due_id, AuthorizationFailure("token=secret"))

    due = await repository.due_for_maintenance(NOW + timedelta(days=2))

    assert due_id not in due
    assert future_id not in due


@pytest.mark.integration
async def test_due_maintenance_returns_each_active_connector_once(database: Database) -> None:
    scope = await create_scope(database)
    repository = ConnectorRepository(database)
    connector_id = await reserve_active(repository, scope)

    due = await repository.due_for_maintenance(NOW + timedelta(days=1))

    assert due.count(connector_id) == 1


@pytest.mark.integration
async def test_renewal_preserves_cursor_and_requires_current_claim(database: Database) -> None:
    scope = await create_scope(database)
    repository = ConnectorRepository(database)
    connector_id = await reserve_active(repository, scope, history_id="durable-before-renewal")
    claim = await repository.claim_sync(connector_id, NOW, lease_seconds=300)
    assert claim is not None
    new_expiration = NOW + timedelta(days=14)
    next_renewal = NOW + timedelta(days=2)

    renewed = await repository.record_watch_renewal(claim, new_expiration, next_renewal)
    stale_renewal = await repository.record_watch_renewal(claim, new_expiration, next_renewal)
    state = await repository.get_sync_state(connector_id)

    assert renewed is True and stale_renewal is False
    assert state is not None
    assert state.history_id == "durable-before-renewal"
    assert state.watch_expiration == new_expiration
    assert state.next_watch_renewal_at == next_renewal
    assert state.claim_id is None


@pytest.mark.integration
async def test_reauthorization_clears_claim_without_resetting_cursor_or_connection(
    database: Database,
) -> None:
    scope = await create_scope(database)
    repository = ConnectorRepository(database)
    connector_id = await reserve_active(repository, scope, history_id="123")
    claim = await repository.claim_sync(connector_id, NOW, lease_seconds=300)
    assert claim is not None

    await repository.mark_reauthorization_required(
        connector_id, AuthorizationFailure("refresh-token=secret message-content")
    )
    connector = await repository.get(connector_id)
    state = await repository.get_sync_state(connector_id)
    async with database.session() as session:
        row = await session.scalar(
            select(ConnectorAccount).where(ConnectorAccount.id == connector_id)
        )

    assert connector is not None and connector.status == ConnectorStatus.REAUTHORIZATION_REQUIRED
    assert connector.connected_at == NOW
    assert state is not None and state.history_id == "123" and state.claim_id is None
    assert row is not None
    assert row.last_error_type == "AuthorizationFailure"
    assert row.last_error_summary == "operation failed"
    stored_error = f"{row.last_error_type}:{row.last_error_summary}"
    assert "secret" not in stored_error
    assert "message-content" not in stored_error
