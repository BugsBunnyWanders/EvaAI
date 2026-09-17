import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import NamedTuple, cast
from uuid import UUID

import pytest
from sqlalchemy import func, select

from eva_ai.actions.errors import ActionScopeError
from eva_ai.actions.repository import ActionRepository
from eva_ai.actions.service import ActionApprovalService
from eva_ai.actions.types import (
    ActionClaimOutcome,
    ActionProposalStatus,
    ActionTaskRequest,
    ApprovalCallbackOutcome,
    ApprovalStatus,
    CreateDraftCompletion,
    GmailActionCapability,
    ManagedDraftStatus,
)
from eva_ai.db import Database
from eva_ai.db.models import (
    Action,
    ActionApproval,
    ActionProposal,
    ManagedGmailDraft,
    Notification,
    OutboxMessage,
)
from eva_ai.notifications.repository import NotificationRepository
from eva_ai.notifications.service import NotificationDeliveryService
from eva_ai.notifications.types import DeliveryOutcome, NotificationDeliveryRequestedMessage
from eva_ai.telegram.contracts import TelegramGateway
from eva_ai.telegram.types import TelegramInlineKeyboardMarkup, TelegramSendResult
from tests.integration.actions.test_repository import _new_create_proposal, _seed_action_scope
from tests.integration.factories import Scope


class PendingApprovalFixture(NamedTuple):
    scope: Scope
    repository: ActionRepository
    completion: CreateDraftCompletion
    telegram_account_id: UUID
    chat_id: int
    now: datetime


async def _pending_approval(
    database: Database,
    *,
    raw_token: str,
    notification_destination: str | None = None,
) -> PendingApprovalFixture:
    scope, connector_id, event_id, telegram_account_id, chat_id = await _seed_action_scope(database)
    repository = ActionRepository(
        database,
        notification_destination=notification_destination,
    )
    now = datetime.now(UTC)
    created = await repository.create_allowed_action(
        proposal=_new_create_proposal(
            scope,
            connector_id,
            event_id,
            now,
            source_key=f"approval-flow:{event_id}",
        ),
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
    return PendingApprovalFixture(
        scope,
        repository,
        completion,
        telegram_account_id,
        chat_id,
        now,
    )


class CapturingTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, TelegramInlineKeyboardMarkup | None, bool]] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: TelegramInlineKeyboardMarkup | None = None,
        preserve_text: bool = False,
    ) -> TelegramSendResult:
        self.calls.append((chat_id, text, reply_markup, preserve_text))
        return TelegramSendResult(message_id=len(self.calls), chat_id=chat_id)


@pytest.mark.integration
async def test_delivery_materializes_raw_token_only_in_telegram_callback_data(
    database: Database,
) -> None:
    seed_token = secrets.token_urlsafe(18)
    scope, _, completion, _, _, now = await _pending_approval(
        database,
        raw_token=seed_token,
        notification_destination="eva-telegram-delivery",
    )
    async with database.session() as session:
        notification = await session.scalar(
            select(Notification).where(
                Notification.action_approval_id == completion.approval.id,
            )
        )
        assert notification is not None
        outbox = await session.scalar(
            select(OutboxMessage).where(
                OutboxMessage.payload["notification_id"].astext == str(notification.id),
            )
        )
        assert outbox is not None

    notifications = NotificationRepository(database)
    telegram = CapturingTelegram()
    service = NotificationDeliveryService(
        notifications=notifications,
        telegram=cast(TelegramGateway, telegram),
        lease_seconds=60,
        max_attempts=3,
        retry_initial_backoff_seconds=1,
        retry_max_backoff_seconds=10,
        clock=lambda: now + timedelta(seconds=3),
    )
    envelope = NotificationDeliveryRequestedMessage(
        outbox_message_id=outbox.id,
        notification_id=notification.id,
        event_id=notification.event_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
    )

    outcome = await service.process(envelope)

    assert outcome is DeliveryOutcome.SENT
    markup = telegram.calls[-1][2]
    assert markup is not None
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    raw_tokens = {callback.split(":", maxsplit=1)[1] for callback in callbacks}
    assert len(raw_tokens) == 1
    delivered_token = raw_tokens.pop()
    async with database.session() as session:
        stored_digest = await session.scalar(
            select(ActionApproval.callback_token_digest).where(
                ActionApproval.id == completion.approval.id,
            )
        )
        stored_notification = await session.get(Notification, notification.id)
        stored_outbox = await session.get(OutboxMessage, outbox.id)
    assert stored_digest == hashlib.sha256(delivered_token.encode()).hexdigest()
    assert stored_notification is not None and stored_outbox is not None
    persisted = (
        f"{stored_notification.message}{stored_notification.reply_markup}{stored_outbox.payload}"
    )
    assert delivered_token not in persisted
    assert all(preserve for _, _, _, preserve in telegram.calls)


@pytest.mark.integration
async def test_send_callback_grants_once_and_queues_one_action(database: Database) -> None:
    raw_token = secrets.token_urlsafe(18)
    scope, repository, completion, telegram_account_id, chat_id, now = await _pending_approval(
        database,
        raw_token=raw_token,
    )
    service = ActionApprovalService(repository, destination="eva-events")

    first = await service.process_callback(
        f"send:{raw_token}",
        telegram_account_id=telegram_account_id,
        chat_id=chat_id,
        now=now + timedelta(minutes=1),
    )
    replay = await service.process_callback(
        f"send:{raw_token}",
        telegram_account_id=telegram_account_id,
        chat_id=chat_id,
        now=now + timedelta(minutes=2),
    )

    assert first.outcome is ApprovalCallbackOutcome.QUEUED
    assert replay.outcome is ApprovalCallbackOutcome.ALREADY_DECIDED
    assert replay.action_id == first.action_id
    async with database.session() as session:
        action_count = await session.scalar(
            select(func.count())
            .select_from(Action)
            .where(Action.proposal_id == completion.send_proposal.id)
        )
    assert action_count == 1
    assert scope.user_id == completion.approval.user_id


@pytest.mark.integration
async def test_discard_invalidates_approval_then_queues_one_exact_delete(
    database: Database,
) -> None:
    raw_token = secrets.token_urlsafe(18)
    _, repository, completion, telegram_account_id, chat_id, now = await _pending_approval(
        database,
        raw_token=raw_token,
    )
    service = ActionApprovalService(repository, destination="eva-events")

    first = await service.process_callback(
        f"discard:{raw_token}",
        telegram_account_id=telegram_account_id,
        chat_id=chat_id,
        now=now + timedelta(minutes=1),
    )
    replay = await service.process_callback(
        f"discard:{raw_token}",
        telegram_account_id=telegram_account_id,
        chat_id=chat_id,
        now=now + timedelta(minutes=2),
    )

    assert first.outcome is ApprovalCallbackOutcome.QUEUED
    assert replay.outcome is ApprovalCallbackOutcome.ALREADY_DECIDED
    assert replay.action_id == first.action_id
    async with database.session() as session:
        approval = await session.get(ActionApproval, completion.approval.id)
        send_proposal = await session.get(ActionProposal, completion.send_proposal.id)
        managed = await session.get(ManagedGmailDraft, completion.managed_draft.id)
        delete_proposals = (
            await session.scalars(
                select(ActionProposal).where(
                    ActionProposal.supersedes_proposal_id == completion.send_proposal.id,
                    ActionProposal.capability == GmailActionCapability.DELETE_DRAFT,
                )
            )
        ).all()
        delete_action_count = await session.scalar(
            select(func.count())
            .select_from(Action)
            .where(Action.proposal_id.in_([proposal.id for proposal in delete_proposals]))
        )
        delete_outbox_count = await session.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(
                OutboxMessage.payload["action_id"].astext == str(first.action_id),
            )
        )
    assert approval is not None and approval.status == ApprovalStatus.REJECTED
    assert send_proposal is not None and send_proposal.status == ActionProposalStatus.CANCELLED
    assert managed is not None and managed.status == ManagedDraftStatus.DELETING
    assert len(delete_proposals) == 1
    assert delete_action_count == 1
    assert delete_outbox_count == 1


@pytest.mark.integration
async def test_wrong_telegram_principal_cannot_transition_approval(database: Database) -> None:
    raw_token = secrets.token_urlsafe(18)
    _, repository, completion, _, chat_id, now = await _pending_approval(
        database,
        raw_token=raw_token,
    )
    service = ActionApprovalService(repository, destination="eva-events")

    with pytest.raises(ActionScopeError, match="unavailable"):
        await service.process_callback(
            f"send:{raw_token}",
            telegram_account_id=completion.approval.id,
            chat_id=chat_id,
            now=now + timedelta(minutes=1),
        )

    stored = await repository.get_approval(
        approval_id=completion.approval.id,
        user_id=completion.approval.user_id,
        workspace_id=completion.approval.workspace_id,
    )
    assert stored is not None and stored.status is ApprovalStatus.PENDING
