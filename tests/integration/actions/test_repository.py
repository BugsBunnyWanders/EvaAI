import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import pytest
from sqlalchemy import func, select, update

from eva_ai.actions.canonical import CanonicalEmail
from eva_ai.actions.errors import ActionScopeError
from eva_ai.actions.repository import ActionRepository
from eva_ai.actions.types import (
    ActionClaimOutcome,
    ActionOrigin,
    ActionProposalStatus,
    ActionStatus,
    ActionTaskRequest,
    ApprovalDecisionOutcome,
    ApprovalStatus,
    GmailActionCapability,
    NewActionProposal,
    PolicyDecision,
)
from eva_ai.connectors.types import ConnectorStatus
from eva_ai.db import Database
from eva_ai.db.models import (
    Action,
    ActionProposal,
    ConnectorAccount,
    Event,
    OutboxMessage,
    TelegramAccount,
)
from eva_ai.events.types import PrincipalType
from eva_ai.telegram.types import TelegramAccountStatus
from tests.integration.factories import Scope, create_scope


def _token_digest() -> str:
    return hashlib.sha256(str(uuid7()).encode()).hexdigest()


async def _seed_action_scope(
    database: Database,
) -> tuple[Scope, UUID, UUID, UUID, int]:
    scope = await create_scope(database)
    connector_id = uuid7()
    event_id = uuid7()
    telegram_account_id = uuid7()
    chat_id = 420_000 + (telegram_account_id.int % 100_000)
    now = datetime.now(UTC)
    async with database.session() as session:
        async with session.begin():
            session.add(
                ConnectorAccount(
                    id=connector_id,
                    user_id=scope.user_id,
                    workspace_id=scope.workspace_id,
                    provider="gmail",
                    account_identity=f"{connector_id}@example.com",
                    granted_scopes=["gmail.readonly", "gmail.compose"],
                    status=ConnectorStatus.ACTIVE,
                    secret_reference=f"secret-{connector_id}",
                    connected_at=now,
                )
            )
            session.add(
                TelegramAccount(
                    id=telegram_account_id,
                    user_id=scope.user_id,
                    workspace_id=scope.workspace_id,
                    telegram_user_id=chat_id,
                    chat_id=chat_id,
                    status=TelegramAccountStatus.ACTIVE,
                    paired_at=now,
                )
            )
            session.add(
                Event(
                    id=event_id,
                    user_id=scope.user_id,
                    workspace_id=scope.workspace_id,
                    source="TELEGRAM",
                    event_type="telegram.message.received",
                    idempotency_key=f"event:{event_id}",
                    occurred_at=now,
                    received_at=now,
                    principal_type=PrincipalType.USER,
                    principal_id=telegram_account_id,
                    payload={"text": "Draft an email"},
                    event_metadata={},
                    correlation_keys=[],
                    schema_version=1,
                )
            )
    return scope, connector_id, event_id, telegram_account_id, chat_id


def _new_create_proposal(
    scope: Scope,
    connector_id: UUID,
    event_id: UUID,
    now: datetime,
    *,
    source_key: str,
) -> NewActionProposal:
    return NewActionProposal(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        connector_account_id=connector_id,
        event_id=event_id,
        origin=ActionOrigin.TELEGRAM_USER,
        capability=GmailActionCapability.CREATE_DRAFT,
        message=CanonicalEmail(
            mode="NEW",
            to=("recipient@example.com",),
            subject="A useful subject",
            text_body="A useful body",
        ),
        description="Create a new Gmail draft",
        policy_decision=PolicyDecision.ALLOW,
        source_key=source_key,
        proposal_family_key=source_key,
        created_at=now,
    )


@pytest.mark.integration
async def test_allowed_action_and_outbox_are_atomic_and_replay_safe(database: Database) -> None:
    scope, connector_id, event_id, _, _ = await _seed_action_scope(database)
    repository = ActionRepository(database)
    now = datetime.now(UTC)
    proposal_input = _new_create_proposal(
        scope, connector_id, event_id, now, source_key=f"test:{uuid7()}"
    )

    first = await repository.create_allowed_action(
        proposal=proposal_input,
        destination="eva-events",
    )
    replay = await repository.create_allowed_action(
        proposal=proposal_input,
        destination="eva-events",
    )

    assert replay == first
    assert first.proposal.status is ActionProposalStatus.QUEUED
    assert first.action.status is ActionStatus.QUEUED
    async with database.session() as session:
        proposal_count = await session.scalar(
            select(func.count())
            .select_from(ActionProposal)
            .where(ActionProposal.id == first.proposal.id)
        )
        action_count = await session.scalar(
            select(func.count()).select_from(Action).where(Action.id == first.action.id)
        )
        outbox_count = await session.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.payload["action_id"].astext == str(first.action.id))
        )
    assert (proposal_count, action_count, outbox_count) == (1, 1, 1)


@pytest.mark.integration
async def test_create_completion_creates_distinct_send_proposal_and_exact_approval(
    database: Database,
) -> None:
    scope, connector_id, event_id, telegram_account_id, chat_id = await _seed_action_scope(database)
    repository = ActionRepository(database)
    now = datetime.now(UTC)
    created = await repository.create_allowed_action(
        proposal=_new_create_proposal(
            scope, connector_id, event_id, now, source_key=f"test:{uuid7()}"
        ),
        destination="eva-events",
    )
    claim_result = await repository.claim_action(
        ActionTaskRequest(action_id=created.action.id), now=now, lease_seconds=60
    )
    assert claim_result.outcome is ActionClaimOutcome.CLAIMED
    assert claim_result.claim is not None
    assert await repository.mark_provider_call_started(
        claim_result.claim, started_at=now + timedelta(seconds=1)
    )
    callback_digest = _token_digest()

    completion = await repository.complete_create_draft(
        claim_result.claim,
        provider_draft_id=f"draft-{uuid7()}",
        provider_message_id=f"message-{uuid7()}",
        provider_thread_id=None,
        rfc_message_id=f"<{uuid7()}@evaatyourservice.com>",
        telegram_account_id=telegram_account_id,
        provider_chat_id=chat_id,
        callback_token_digest=callback_digest,
        approval_expires_at=now + timedelta(hours=24),
        completed_at=now + timedelta(seconds=2),
    )

    assert completion.send_proposal.id != created.proposal.id
    assert completion.send_proposal.capability is GmailActionCapability.SEND_DRAFT
    assert completion.send_proposal.status is ActionProposalStatus.WAITING_APPROVAL
    assert completion.approval.status is ApprovalStatus.PENDING
    assert completion.approval.parameters_hash == completion.managed_draft.current_content_hash
    assert completion.managed_draft.active_send_proposal_id == completion.send_proposal.id


@pytest.mark.integration
async def test_exact_approval_queues_one_send_and_replay_returns_it(database: Database) -> None:
    scope, connector_id, event_id, telegram_account_id, chat_id = await _seed_action_scope(database)
    repository = ActionRepository(database)
    now = datetime.now(UTC)
    created = await repository.create_allowed_action(
        proposal=_new_create_proposal(
            scope, connector_id, event_id, now, source_key=f"test:{uuid7()}"
        ),
        destination="eva-events",
    )
    claim_result = await repository.claim_action(
        ActionTaskRequest(action_id=created.action.id), now=now, lease_seconds=60
    )
    assert claim_result.claim is not None
    await repository.mark_provider_call_started(
        claim_result.claim, started_at=now + timedelta(seconds=1)
    )
    callback_digest = _token_digest()
    completion = await repository.complete_create_draft(
        claim_result.claim,
        provider_draft_id=f"draft-{uuid7()}",
        provider_message_id=f"message-{uuid7()}",
        provider_thread_id=None,
        rfc_message_id=f"<{uuid7()}@evaatyourservice.com>",
        telegram_account_id=telegram_account_id,
        provider_chat_id=chat_id,
        callback_token_digest=callback_digest,
        approval_expires_at=now + timedelta(hours=24),
        completed_at=now + timedelta(seconds=2),
    )

    first = await repository.grant_and_queue_send(
        callback_token_digest=callback_digest,
        telegram_account_id=telegram_account_id,
        chat_id=chat_id,
        destination="eva-events",
        now=now + timedelta(minutes=1),
    )
    replay = await repository.grant_and_queue_send(
        callback_token_digest=callback_digest,
        telegram_account_id=telegram_account_id,
        chat_id=chat_id,
        destination="eva-events",
        now=now + timedelta(minutes=2),
    )

    assert first.outcome is ApprovalDecisionOutcome.QUEUED
    assert first.action is not None
    assert replay.outcome is ApprovalDecisionOutcome.ALREADY_DECIDED
    assert replay.action == first.action
    persisted = await repository.get_approval(
        approval_id=completion.approval.id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
    )
    assert persisted is not None
    assert persisted.status is ApprovalStatus.GRANTED


@pytest.mark.integration
async def test_expired_approval_never_queues_send(database: Database) -> None:
    scope, connector_id, event_id, telegram_account_id, chat_id = await _seed_action_scope(database)
    repository = ActionRepository(database)
    now = datetime.now(UTC)
    created = await repository.create_allowed_action(
        proposal=_new_create_proposal(
            scope, connector_id, event_id, now, source_key=f"test:{uuid7()}"
        ),
        destination="eva-events",
    )
    claim_result = await repository.claim_action(
        ActionTaskRequest(action_id=created.action.id), now=now, lease_seconds=60
    )
    assert claim_result.claim is not None
    await repository.mark_provider_call_started(
        claim_result.claim, started_at=now + timedelta(seconds=1)
    )
    callback_digest = _token_digest()
    completion = await repository.complete_create_draft(
        claim_result.claim,
        provider_draft_id=f"draft-{uuid7()}",
        provider_message_id=f"message-{uuid7()}",
        provider_thread_id=None,
        rfc_message_id=f"<{uuid7()}@evaatyourservice.com>",
        telegram_account_id=telegram_account_id,
        provider_chat_id=chat_id,
        callback_token_digest=callback_digest,
        approval_expires_at=now + timedelta(minutes=1),
        completed_at=now + timedelta(seconds=2),
    )

    decision = await repository.grant_and_queue_send(
        callback_token_digest=callback_digest,
        telegram_account_id=telegram_account_id,
        chat_id=chat_id,
        destination="eva-events",
        now=now + timedelta(minutes=2),
    )

    assert decision.outcome is ApprovalDecisionOutcome.EXPIRED
    assert decision.action is None
    async with database.session() as session:
        action_count = await session.scalar(
            select(func.count())
            .select_from(Action)
            .where(Action.proposal_id == completion.send_proposal.id)
        )
    assert action_count == 0


@pytest.mark.integration
async def test_expired_lease_after_provider_boundary_becomes_unknown(database: Database) -> None:
    scope, connector_id, event_id, _, _ = await _seed_action_scope(database)
    repository = ActionRepository(database)
    now = datetime.now(UTC)
    created = await repository.create_allowed_action(
        proposal=_new_create_proposal(
            scope, connector_id, event_id, now, source_key=f"test:{uuid7()}"
        ),
        destination="eva-events",
    )
    first = await repository.claim_action(
        ActionTaskRequest(action_id=created.action.id), now=now, lease_seconds=60
    )
    assert first.claim is not None
    await repository.mark_provider_call_started(first.claim, started_at=now + timedelta(seconds=1))
    async with database.session() as session:
        async with session.begin():
            await session.execute(
                update(Action)
                .where(Action.id == created.action.id)
                .values(lease_expires_at=now - timedelta(seconds=1))
            )

    replay = await repository.claim_action(
        ActionTaskRequest(action_id=created.action.id),
        now=now + timedelta(minutes=2),
        lease_seconds=60,
    )

    assert replay.outcome is ActionClaimOutcome.UNKNOWN
    assert replay.claim is None
    stored = await repository.get_action(created.action.id)
    assert stored is not None
    assert stored.status is ActionStatus.UNKNOWN


@pytest.mark.integration
async def test_expired_lease_before_provider_boundary_is_reclaimable(database: Database) -> None:
    scope, connector_id, event_id, _, _ = await _seed_action_scope(database)
    repository = ActionRepository(database)
    now = datetime.now(UTC)
    created = await repository.create_allowed_action(
        proposal=_new_create_proposal(
            scope, connector_id, event_id, now, source_key=f"test:{uuid7()}"
        ),
        destination="eva-events",
    )
    first = await repository.claim_action(
        ActionTaskRequest(action_id=created.action.id), now=now, lease_seconds=1
    )
    replay = await repository.claim_action(
        ActionTaskRequest(action_id=created.action.id),
        now=now + timedelta(seconds=2),
        lease_seconds=60,
    )

    assert first.claim is not None
    assert replay.outcome is ActionClaimOutcome.CLAIMED
    assert replay.claim is not None
    assert replay.claim.claim_id != first.claim.claim_id
    assert replay.claim.attempt_count == 2


@pytest.mark.integration
async def test_cross_scope_proposal_is_rejected_without_partial_rows(database: Database) -> None:
    first_scope, connector_id, event_id, _, _ = await _seed_action_scope(database)
    other_scope = await create_scope(database)
    repository = ActionRepository(database)
    now = datetime.now(UTC)
    source_key = f"test:{uuid7()}"
    cross_scope = _new_create_proposal(
        other_scope,
        connector_id,
        event_id,
        now,
        source_key=source_key,
    )

    with pytest.raises(ActionScopeError, match="scope"):
        await repository.create_allowed_action(
            proposal=cross_scope,
            destination="eva-events",
        )

    async with database.session() as session:
        proposal_count = await session.scalar(
            select(func.count())
            .select_from(ActionProposal)
            .where(ActionProposal.source_key == source_key)
        )
    assert proposal_count == 0
    assert first_scope != other_scope


@pytest.mark.integration
async def test_superseded_approval_cannot_queue_send(database: Database) -> None:
    scope, connector_id, event_id, telegram_account_id, chat_id = await _seed_action_scope(database)
    repository = ActionRepository(database)
    now = datetime.now(UTC)
    created = await repository.create_allowed_action(
        proposal=_new_create_proposal(
            scope, connector_id, event_id, now, source_key=f"test:{uuid7()}"
        ),
        destination="eva-events",
    )
    claim_result = await repository.claim_action(
        ActionTaskRequest(action_id=created.action.id), now=now, lease_seconds=60
    )
    assert claim_result.claim is not None
    await repository.mark_provider_call_started(
        claim_result.claim, started_at=now + timedelta(seconds=1)
    )
    callback_digest = _token_digest()
    completion = await repository.complete_create_draft(
        claim_result.claim,
        provider_draft_id=f"draft-{uuid7()}",
        provider_message_id=f"message-{uuid7()}",
        provider_thread_id=None,
        rfc_message_id=f"<{uuid7()}@evaatyourservice.com>",
        telegram_account_id=telegram_account_id,
        provider_chat_id=chat_id,
        callback_token_digest=callback_digest,
        approval_expires_at=now + timedelta(hours=24),
        completed_at=now + timedelta(seconds=2),
    )

    superseded = await repository.supersede_send_approval(
        callback_token_digest=callback_digest,
        telegram_account_id=telegram_account_id,
        chat_id=chat_id,
        now=now + timedelta(minutes=1),
    )
    decision = await repository.grant_and_queue_send(
        callback_token_digest=callback_digest,
        telegram_account_id=telegram_account_id,
        chat_id=chat_id,
        destination="eva-events",
        now=now + timedelta(minutes=2),
    )

    assert superseded.status is ApprovalStatus.SUPERSEDED
    assert decision.outcome is ApprovalDecisionOutcome.ALREADY_DECIDED
    assert decision.action is None
    async with database.session() as session:
        proposal_status = await session.scalar(
            select(ActionProposal.status).where(ActionProposal.id == completion.send_proposal.id)
        )
    assert proposal_status == ActionProposalStatus.SUPERSEDED
