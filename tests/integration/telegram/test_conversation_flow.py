import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

import pytest
from sqlalchemy import select

from eva_ai.agent.types import AgentUsage, ProposedAction
from eva_ai.conversation.repository import ConversationRepository
from eva_ai.db import Database
from eva_ai.db.models import OutboxMessage
from eva_ai.memory.types import (
    MemoryProposal,
    MemoryProposalKind,
    MemoryScopeType,
    MemorySourceType,
)
from eva_ai.notifications.repository import NotificationRepository
from eva_ai.notifications.types import NotificationDeliveryRequestedMessage, NotificationStatus
from eva_ai.telegram.ingestion import TelegramEventService
from eva_ai.telegram.repository import TelegramAccountRepository
from eva_ai.telegram.types import (
    PairingConsumeCommand,
    TelegramTurnRequestedMessage,
    TelegramUpdate,
    WebhookDisposition,
)
from eva_ai.telegram.webhook import TelegramWebhookService
from tests.integration.factories import create_scope

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _update(
    update_id: int,
    text: str,
    *,
    telegram_user_id: int,
    reply_to: int | None = None,
) -> TelegramUpdate:
    message: dict[str, object] = {
        "message_id": update_id + 100,
        "date": int((NOW + timedelta(seconds=update_id)).timestamp()),
        "chat": {"id": telegram_user_id, "type": "private"},
        "from": {"id": telegram_user_id, "is_bot": False, "first_name": "User"},
        "text": text,
    }
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return TelegramUpdate.model_validate({"update_id": update_id, "message": message})


async def _turn_envelope(database: Database, event_id: object) -> TelegramTurnRequestedMessage:
    async with database.session() as session:
        payload = await session.scalar(
            select(OutboxMessage.payload).where(
                OutboxMessage.event_id == event_id,
                OutboxMessage.message_type == "telegram.turn.requested",
            )
        )
    assert payload is not None
    return TelegramTurnRequestedMessage.model_validate(payload)


@pytest.mark.integration
async def test_pairing_chat_reply_and_new_conversation_are_user_scoped(
    database: Database,
) -> None:
    scope = await create_scope(database)
    telegram_user_id = uuid7().int % 1_000_000_000 + 1
    accounts = TelegramAccountRepository(database)
    link = await accounts.create_pairing(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        bot_username="EvaTestBot",
        now=NOW,
        ttl_seconds=900,
    )
    account = await accounts.consume_pairing(
        PairingConsumeCommand(
            token=link.token,
            telegram_user_id=telegram_user_id,
            chat_id=telegram_user_id,
            first_name="User",
            consumed_at=NOW + timedelta(seconds=1),
        )
    )
    webhook = TelegramWebhookService(accounts, TelegramEventService(database, "eva-telegram-turns"))
    conversations = ConversationRepository(database, "eva-telegram-delivery")

    first_ingest = await webhook.handle(
        _update(1, "What needs my attention?", telegram_user_id=account.telegram_user_id),
        received_at=NOW + timedelta(seconds=2),
    )
    assert first_ingest.disposition is WebhookDisposition.INGESTED
    assert first_ingest.event_id is not None
    first_envelope = await _turn_envelope(database, first_ingest.event_id)
    first_claim = await conversations.resolve_and_claim(
        first_envelope,
        now=NOW + timedelta(seconds=3),
        lease_seconds=300,
        agent_version="conversation-v1",
    )
    assert first_claim is not None
    first_subject = await conversations.load_subject(
        first_claim, history_limit=20, history_max_chars=24000
    )
    assistant = await conversations.complete(
        first_claim,
        response_text="You have one relevant recruiting email.",
        agent_version="conversation-v1",
        provider_response_id="response-1",
        usage=AgentUsage(input_tokens=10, output_tokens=8, total_tokens=18),
        tool_audit=(),
        reasoning_summary="The user asked for an attention summary.",
        proposed_actions=(
            ProposedAction(
                capability="review_email",
                description="Review the relevant recruiting email.",
            ),
        ),
        memory_proposals=(
            MemoryProposal(
                kind=MemoryProposalKind.FACT,
                claim="The user is exploring recruiting opportunities.",
                namespace="career",
                key="job_search_status",
                scope_type=MemoryScopeType.WORKSPACE,
                scope_id=scope.workspace_id,
                source_type=MemorySourceType.AGENT_INFERRED,
                source_ref="conversation:test",
                confidence=Decimal("0.9"),
                reason="Useful for future career assistance.",
            ),
        ),
        completed_at=NOW + timedelta(seconds=4),
    )
    assert assistant.notification_id is not None
    assert assistant.reasoning_summary == "The user asked for an attention summary."
    assert assistant.proposed_actions[0].capability == "review_email"
    assert assistant.memory_proposals[0].key == "job_search_status"
    notifications = NotificationRepository(database)
    notification = await notifications.get(
        notification_id=assistant.notification_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
    )
    assert notification is not None
    delivery = NotificationDeliveryRequestedMessage(
        outbox_message_id=uuid7(),
        notification_id=notification.id,
        event_id=notification.event_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
    )
    delivery_claim = await notifications.claim(
        delivery, now=NOW + timedelta(seconds=5), lease_seconds=300
    )
    assert delivery_claim is not None
    await notifications.mark_sent(
        delivery_claim,
        telegram_account_id=account.id,
        chat_id=account.chat_id,
        provider_message_id=501,
        sent_at=NOW + timedelta(seconds=6),
    )

    reply_ingest = await webhook.handle(
        _update(
            2,
            "Tell me about it",
            telegram_user_id=account.telegram_user_id,
            reply_to=501,
        ),
        received_at=NOW + timedelta(seconds=7),
    )
    assert reply_ingest.event_id is not None
    reply_claim = await conversations.resolve_and_claim(
        await _turn_envelope(database, reply_ingest.event_id),
        now=NOW + timedelta(seconds=8),
        lease_seconds=300,
        agent_version="conversation-v1",
    )
    assert reply_claim is not None
    reply_subject = await conversations.load_subject(
        reply_claim, history_limit=20, history_max_chars=24000
    )
    assert reply_subject.conversation.situation_id == first_subject.conversation.situation_id
    assert [turn.text for turn in reply_subject.history][-1] == notification.message

    new_ingest = await webhook.handle(
        _update(3, "/new", telegram_user_id=account.telegram_user_id),
        received_at=NOW + timedelta(seconds=9),
    )
    assert new_ingest.event_id is not None
    new_claim = await conversations.resolve_and_claim(
        await _turn_envelope(database, new_ingest.event_id),
        now=NOW + timedelta(seconds=10),
        lease_seconds=300,
        agent_version="conversation-v1",
    )
    assert new_claim is not None
    new_subject = await conversations.load_subject(
        new_claim, history_limit=20, history_max_chars=24000
    )
    assert new_subject.conversation.situation_id != first_subject.conversation.situation_id

    cross_scope = await create_scope(database)
    assert (
        await notifications.get(
            notification_id=notification.id,
            user_id=cross_scope.user_id,
            workspace_id=cross_scope.workspace_id,
        )
        is None
    )
    assert notification.status is NotificationStatus.PENDING


@pytest.mark.integration
async def test_pairing_code_is_single_use(database: Database) -> None:
    scope = await create_scope(database)
    telegram_user_id = uuid7().int % 1_000_000_000 + 1
    accounts = TelegramAccountRepository(database)
    link = await accounts.create_pairing(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        bot_username="EvaTestBot",
        now=NOW,
        ttl_seconds=900,
    )
    command = PairingConsumeCommand(
        token=link.token,
        telegram_user_id=telegram_user_id,
        chat_id=telegram_user_id,
        consumed_at=NOW + timedelta(seconds=1),
    )
    await accounts.consume_pairing(command)

    from eva_ai.telegram.errors import PairingCodeInvalidError

    with pytest.raises(PairingCodeInvalidError):
        await accounts.consume_pairing(command)


@pytest.mark.integration
async def test_rapid_first_messages_share_one_conversation_and_one_running_turn(
    database: Database,
) -> None:
    scope = await create_scope(database)
    telegram_user_id = uuid7().int % 1_000_000_000 + 1
    accounts = TelegramAccountRepository(database)
    link = await accounts.create_pairing(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        bot_username="EvaTestBot",
        now=NOW,
        ttl_seconds=900,
    )
    account = await accounts.consume_pairing(
        PairingConsumeCommand(
            token=link.token,
            telegram_user_id=telegram_user_id,
            chat_id=telegram_user_id,
            consumed_at=NOW + timedelta(seconds=1),
        )
    )
    webhook = TelegramWebhookService(accounts, TelegramEventService(database, "eva-telegram-turns"))
    conversations = ConversationRepository(database, "eva-telegram-delivery")
    first, second = await asyncio.gather(
        webhook.handle(
            _update(20, "First message", telegram_user_id=account.telegram_user_id),
            received_at=NOW + timedelta(seconds=2),
        ),
        webhook.handle(
            _update(21, "Second message", telegram_user_id=account.telegram_user_id),
            received_at=NOW + timedelta(seconds=3),
        ),
    )
    assert first.event_id is not None and second.event_id is not None
    claims = await asyncio.gather(
        conversations.resolve_and_claim(
            await _turn_envelope(database, first.event_id),
            now=NOW + timedelta(seconds=4),
            lease_seconds=300,
            agent_version="conversation-v1",
        ),
        conversations.resolve_and_claim(
            await _turn_envelope(database, second.event_id),
            now=NOW + timedelta(seconds=4),
            lease_seconds=300,
            agent_version="conversation-v1",
        ),
    )

    first_turn = await conversations.find_turn_for_event(
        event_id=first.event_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
    )
    second_turn = await conversations.find_turn_for_event(
        event_id=second.event_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
    )
    assert first_turn is not None and second_turn is not None
    assert first_turn.conversation_id == second_turn.conversation_id
    assert sum(claim is not None for claim in claims) == 1
