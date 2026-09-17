import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest
from sqlalchemy import select

from eva_ai.actions.repository import ActionRepository
from eva_ai.actions.service import ActionApprovalService, ActionRevisionService
from eva_ai.actions.types import (
    ActionClaimOutcome,
    ActionProposalStatus,
    ActionTaskRequest,
    ApprovalCallbackOutcome,
    ApprovalStatus,
    DraftRevisionCandidate,
    GmailActionCapability,
    RevisionLookupStatus,
    RevisionSessionStatus,
)
from eva_ai.db import Database
from eva_ai.db.models import (
    ActionApproval,
    ActionProposal,
    ActionRevisionSession,
    Situation,
    TelegramConversation,
)
from eva_ai.situations.types import AttentionLevel, SituationLifecycle, SituationType
from tests.integration.actions.test_repository import _new_create_proposal, _seed_action_scope


@pytest.mark.integration
async def test_change_supersedes_exact_approval_and_opens_bounded_revision(
    database: Database,
) -> None:
    scope, connector_id, event_id, telegram_account_id, chat_id = await _seed_action_scope(database)
    now = datetime.now(UTC)
    situation_id = uuid7()
    async with database.session() as session:
        async with session.begin():
            session.add(
                Situation(
                    id=situation_id,
                    user_id=scope.user_id,
                    workspace_id=scope.workspace_id,
                    type=SituationType.TELEGRAM_CHAT,
                    title="Draft request",
                    lifecycle=SituationLifecycle.ACTIVE,
                    attention=AttentionLevel.NORMAL,
                    summary="",
                    current_state="DRAFTING",
                    next_action=None,
                    next_expected=None,
                    version=1,
                    last_activity_at=now,
                )
            )

    repository = ActionRepository(database, notification_destination="eva-telegram-delivery")
    create_proposal = _new_create_proposal(
        scope,
        connector_id,
        event_id,
        now,
        source_key=f"revision:{event_id}",
    ).model_copy(update={"situation_id": situation_id})
    created = await repository.create_allowed_action(
        proposal=create_proposal,
        destination="eva-events",
    )
    claimed = await repository.claim_action(
        ActionTaskRequest(action_id=created.action.id),
        now=now,
        lease_seconds=60,
    )
    assert claimed.outcome is ActionClaimOutcome.CLAIMED
    assert claimed.claim is not None
    await repository.mark_provider_call_started(
        claimed.claim,
        started_at=now + timedelta(seconds=1),
    )
    raw_token = secrets.token_urlsafe(18)
    completion = await repository.complete_create_draft(
        claimed.claim,
        provider_draft_id=f"draft-{event_id}",
        provider_message_id=f"message-{event_id}",
        provider_thread_id=None,
        rfc_message_id=f"<{event_id}@evaatyourservice.com>",
        telegram_account_id=telegram_account_id,
        provider_chat_id=chat_id,
        callback_token_digest=hashlib.sha256(raw_token.encode()).hexdigest(),
        approval_expires_at=now + timedelta(hours=24),
        completed_at=now + timedelta(seconds=2),
    )
    service = ActionApprovalService(
        repository,
        destination="eva-events",
        revision_ttl=timedelta(minutes=15),
    )

    result = await service.process_callback(
        f"change:{raw_token}",
        telegram_account_id=telegram_account_id,
        chat_id=chat_id,
        now=now + timedelta(minutes=1),
    )

    assert result.outcome is ApprovalCallbackOutcome.CHANGE_REQUESTED
    async with database.session() as session:
        approval = await session.get(ActionApproval, completion.approval.id)
        proposal = await session.get(ActionProposal, completion.send_proposal.id)
        revision = await session.scalar(
            select(ActionRevisionSession).where(
                ActionRevisionSession.telegram_account_id == telegram_account_id,
                ActionRevisionSession.status == RevisionSessionStatus.ACTIVE,
            )
        )
        conversation = (
            None
            if revision is None
            else await session.get(TelegramConversation, revision.conversation_id)
        )
    assert approval is not None and approval.status == ApprovalStatus.SUPERSEDED
    assert proposal is not None and proposal.status == ActionProposalStatus.SUPERSEDED
    assert revision is not None
    assert revision.managed_draft_id == completion.managed_draft.id
    assert revision.expires_at == now + timedelta(minutes=16)
    assert conversation is not None and conversation.situation_id == situation_id

    lookup = await repository.load_revision(
        telegram_account_id=telegram_account_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        now=now + timedelta(minutes=2),
    )
    assert lookup.status is RevisionLookupStatus.ACTIVE
    assert lookup.context is not None
    assert lookup.context.current_message == completion.send_proposal.message

    revision_service = ActionRevisionService(repository, destination="eva-events")
    queued = await revision_service.complete(
        session_id=lookup.context.session_id,
        current=lookup.context.current_message,
        candidate=DraftRevisionCandidate(
            mode="NEW",
            to=("changed@example.com",),
            subject="A revised subject",
            text_body="A completely revised body.",
        ),
        now=now + timedelta(minutes=3),
    )

    assert queued.proposal.capability is GmailActionCapability.UPDATE_DRAFT
    assert queued.proposal.supersedes_proposal_id == completion.send_proposal.id
    async with database.session() as session:
        stored_revision = await session.get(ActionRevisionSession, revision.id)
    assert stored_revision is not None
    assert stored_revision.status == RevisionSessionStatus.COMPLETED
    old_callback = await service.process_callback(
        f"send:{raw_token}",
        telegram_account_id=telegram_account_id,
        chat_id=chat_id,
        now=now + timedelta(minutes=4),
    )
    assert old_callback.outcome is ApprovalCallbackOutcome.ALREADY_DECIDED

    update_claim = await repository.claim_action(
        ActionTaskRequest(action_id=queued.action.id),
        now=now + timedelta(minutes=4),
        lease_seconds=60,
    )
    assert update_claim.outcome is ActionClaimOutcome.CLAIMED
    assert update_claim.claim is not None
    await repository.mark_provider_call_started(
        update_claim.claim,
        started_at=now + timedelta(minutes=4, seconds=1),
    )
    refreshed = await repository.complete_update_draft(
        update_claim.claim,
        provider_draft_id=completion.managed_draft.provider_draft_id,
        provider_message_id="updated-provider-message",
        provider_thread_id=None,
        callback_token_digest=hashlib.sha256(secrets.token_bytes(24)).hexdigest(),
        telegram_account_id=telegram_account_id,
        provider_chat_id=chat_id,
        approval_expires_at=now + timedelta(hours=24),
        completed_at=now + timedelta(minutes=5),
    )
    assert refreshed.managed_draft.provider_draft_id == completion.managed_draft.provider_draft_id
    assert refreshed.send_proposal.id != completion.send_proposal.id
    assert refreshed.send_proposal.message.text_body == "A completely revised body."
    assert refreshed.approval.status is ApprovalStatus.PENDING
