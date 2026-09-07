from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid5, uuid7

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from eva_ai.agent.types import AgentUsage, NotificationUrgency, ToolCallAudit
from eva_ai.connectors.types import ConnectorStatus
from eva_ai.conversation.errors import ConversationConflictError, ConversationScopeError
from eva_ai.conversation.types import (
    ConversationHistoryTurn,
    ConversationKind,
    ConversationRecord,
    ConversationStatus,
    ConversationTurnRecord,
    ConversationTurnRole,
    ConversationTurnStatus,
)
from eva_ai.db.models import (
    ConnectorAccount,
    ConversationTurn,
    Event,
    EventProcessing,
    Notification,
    Situation,
    SituationCorrelationKey,
    SituationEvent,
    TelegramAccount,
    TelegramConversation,
)
from eva_ai.db.session import Database
from eva_ai.events.types import ProcessingStage
from eva_ai.notifications.repository import NotificationRepository
from eva_ai.notifications.types import NotificationKind
from eva_ai.situations.types import (
    AttentionLevel,
    CorrelationKeyKind,
    CorrelationMethod,
    SituationLifecycle,
    SituationType,
)
from eva_ai.telegram.types import TelegramAccountStatus, TelegramTurnRequestedMessage

_GENERAL_SITUATION_NAMESPACE = UUID("59333d11-3135-54cd-b622-afb99ad6ad58")
_CONVERSATION_NAMESPACE = UUID("5307d944-4cb9-52aa-b66f-69010b43345a")
_TURN_NAMESPACE = UUID("0b85e2f8-5738-5006-b583-4f3a32d184cc")


@dataclass(frozen=True, slots=True)
class ConversationTurnClaim:
    turn_id: UUID
    conversation_id: UUID
    claim_id: UUID
    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    attempt_count: int


@dataclass(frozen=True, slots=True)
class ConversationTurnSubject:
    conversation: ConversationRecord
    turn: ConversationTurnRecord
    history: tuple[ConversationHistoryTurn, ...]
    situation_type: SituationType
    connector_id: UUID | None
    secret_reference: str | None
    gmail_thread_id: str | None


class ConversationRepository:
    def __init__(self, database: Database, notification_destination: str) -> None:
        self._database = database
        self._notifications = NotificationRepository(database)
        self._notification_destination = notification_destination

    async def resolve_and_claim(
        self,
        message: TelegramTurnRequestedMessage,
        *,
        now: datetime,
        lease_seconds: int,
        agent_version: str,
    ) -> ConversationTurnClaim | None:
        async with self._database.session() as session:
            async with session.begin():
                event = await session.scalar(
                    select(Event).where(
                        Event.id == message.event_id,
                        Event.user_id == message.user_id,
                        Event.workspace_id == message.workspace_id,
                        Event.source == "TELEGRAM",
                        Event.event_type == "telegram.message.received",
                    )
                )
                if event is None or event.principal_id is None:
                    raise ConversationScopeError("Telegram message Event is unavailable")
                account = await session.scalar(
                    select(TelegramAccount)
                    .where(
                        TelegramAccount.id == event.principal_id,
                        TelegramAccount.user_id == message.user_id,
                        TelegramAccount.workspace_id == message.workspace_id,
                        TelegramAccount.status == TelegramAccountStatus.ACTIVE,
                    )
                    .with_for_update()
                )
                if account is None:
                    raise ConversationScopeError("Telegram account is unavailable")

                existing = await session.scalar(
                    select(ConversationTurn)
                    .where(
                        ConversationTurn.event_id == event.id,
                        ConversationTurn.user_id == message.user_id,
                        ConversationTurn.workspace_id == message.workspace_id,
                    )
                    .with_for_update()
                )
                if existing is None:
                    conversation = await self._resolve_conversation(
                        session, event=event, account=account, now=now
                    )
                    locked_conversation = await session.scalar(
                        select(TelegramConversation)
                        .where(TelegramConversation.id == conversation.id)
                        .with_for_update()
                    )
                    if locked_conversation is None:
                        raise ConversationConflictError("Conversation was not visible")
                    conversation = locked_conversation
                    existing = ConversationTurn(
                        id=uuid5(_TURN_NAMESPACE, f"event:{event.id}"),
                        user_id=event.user_id,
                        workspace_id=event.workspace_id,
                        conversation_id=conversation.id,
                        role=ConversationTurnRole.USER,
                        status=ConversationTurnStatus.QUEUED,
                        sequence=conversation.next_sequence,
                        text=_event_text(event),
                        event_id=event.id,
                        notification_id=None,
                        agent_version=agent_version,
                        attempt_count=0,
                    )
                    conversation.next_sequence += 1
                    conversation.last_activity_at = now
                    session.add(existing)
                    session.add(
                        SituationEvent(
                            situation_id=conversation.situation_id,
                            event_id=event.id,
                            workspace_id=event.workspace_id,
                            user_id=event.user_id,
                            correlation_method=CorrelationMethod.EXPLICIT,
                            correlation_key=f"telegram:chat:{account.chat_id}",
                            linked_at=now,
                        )
                    )
                    await session.flush()

                competing = await session.scalar(
                    select(ConversationTurn.id).where(
                        ConversationTurn.conversation_id == existing.conversation_id,
                        ConversationTurn.role == ConversationTurnRole.USER,
                        ConversationTurn.status == ConversationTurnStatus.RUNNING,
                        ConversationTurn.id != existing.id,
                    )
                )
                if competing is not None:
                    return None
                eligible = existing.status in {
                    ConversationTurnStatus.QUEUED,
                    ConversationTurnStatus.RETRYABLE_FAILURE,
                } or (
                    existing.status == ConversationTurnStatus.RUNNING
                    and existing.lease_expires_at is not None
                    and existing.lease_expires_at <= now
                )
                if not eligible or (
                    existing.next_retry_at is not None and existing.next_retry_at > now
                ):
                    return None
                claim_id = uuid7()
                existing.status = ConversationTurnStatus.RUNNING
                existing.claim_id = claim_id
                existing.lease_expires_at = now + timedelta(seconds=lease_seconds)
                existing.attempt_count += 1
                existing.next_retry_at = None
                existing.failure_code = None
                existing.failure_summary = None
                await session.flush()
                return ConversationTurnClaim(
                    turn_id=existing.id,
                    conversation_id=existing.conversation_id,
                    claim_id=claim_id,
                    event_id=event.id,
                    user_id=event.user_id,
                    workspace_id=event.workspace_id,
                    attempt_count=existing.attempt_count,
                )

    async def mark_callback_handled(
        self, message: TelegramTurnRequestedMessage, *, processed_at: datetime
    ) -> bool:
        statement = (
            update(EventProcessing)
            .where(
                EventProcessing.event_id == message.event_id,
                EventProcessing.event_id.in_(
                    select(Event.id).where(
                        Event.id == message.event_id,
                        Event.user_id == message.user_id,
                        Event.workspace_id == message.workspace_id,
                        Event.source == "TELEGRAM",
                        Event.event_type == "telegram.callback.received",
                    )
                ),
            )
            .values(stage=ProcessingStage.HANDLED, processed_at=processed_at)
            .returning(EventProcessing.event_id)
        )
        async with self._database.session() as session:
            async with session.begin():
                return (await session.scalar(statement)) is not None

    async def event_type(self, message: TelegramTurnRequestedMessage) -> str | None:
        statement = select(Event.event_type).where(
            Event.id == message.event_id,
            Event.user_id == message.user_id,
            Event.workspace_id == message.workspace_id,
            Event.source == "TELEGRAM",
        )
        async with self._database.session() as session:
            value = await session.scalar(statement)
        return value if isinstance(value, str) else None

    async def load_subject(
        self, claim: ConversationTurnClaim, *, history_limit: int, history_max_chars: int
    ) -> ConversationTurnSubject:
        statement = (
            select(ConversationTurn, TelegramConversation, Situation)
            .join(
                TelegramConversation,
                and_(
                    TelegramConversation.id == ConversationTurn.conversation_id,
                    TelegramConversation.user_id == ConversationTurn.user_id,
                    TelegramConversation.workspace_id == ConversationTurn.workspace_id,
                ),
            )
            .join(
                Situation,
                and_(
                    Situation.id == TelegramConversation.situation_id,
                    Situation.user_id == TelegramConversation.user_id,
                    Situation.workspace_id == TelegramConversation.workspace_id,
                ),
            )
            .where(_claim_predicate(claim))
        )
        async with self._database.session() as session:
            values = (await session.execute(statement)).one_or_none()
            if values is None:
                raise ConversationScopeError("claimed conversation turn is unavailable")
            turn, conversation, situation = values
            history_rows = (
                await session.scalars(
                    select(ConversationTurn)
                    .where(
                        ConversationTurn.conversation_id == conversation.id,
                        ConversationTurn.sequence < turn.sequence,
                        ConversationTurn.status == ConversationTurnStatus.SUCCEEDED,
                    )
                    .order_by(ConversationTurn.sequence.desc())
                    .limit(history_limit)
                )
            ).all()
            history = _bounded_history(tuple(reversed(history_rows)), history_max_chars)
            connector = await session.scalar(
                select(ConnectorAccount)
                .where(
                    ConnectorAccount.user_id == claim.user_id,
                    ConnectorAccount.workspace_id == claim.workspace_id,
                    ConnectorAccount.provider == "gmail",
                    ConnectorAccount.status == ConnectorStatus.ACTIVE,
                )
                .order_by(ConnectorAccount.created_at.desc())
                .limit(1)
            )
            thread_id = None
            if SituationType(situation.type) is SituationType.EMAIL_THREAD:
                thread_id = await session.scalar(
                    select(SituationCorrelationKey.correlation_key).where(
                        SituationCorrelationKey.situation_id == situation.id,
                        SituationCorrelationKey.user_id == claim.user_id,
                        SituationCorrelationKey.workspace_id == claim.workspace_id,
                        SituationCorrelationKey.kind == CorrelationKeyKind.GMAIL_THREAD,
                    )
                )
        return ConversationTurnSubject(
            conversation=_conversation_record(conversation),
            turn=_turn_record(turn),
            history=history,
            situation_type=SituationType(situation.type),
            connector_id=None if connector is None else connector.id,
            secret_reference=None if connector is None else connector.secret_reference,
            gmail_thread_id=thread_id,
        )

    async def complete(
        self,
        claim: ConversationTurnClaim,
        *,
        response_text: str,
        agent_version: str,
        provider_response_id: str | None,
        usage: AgentUsage,
        tool_audit: tuple[ToolCallAudit, ...],
        completed_at: datetime,
    ) -> ConversationTurnRecord:
        async with self._database.session() as session:
            async with session.begin():
                user_turn = await session.scalar(
                    select(ConversationTurn).where(_claim_predicate(claim)).with_for_update()
                )
                if user_turn is None:
                    raise ConversationConflictError("conversation claim is stale")
                conversation = await session.scalar(
                    select(TelegramConversation)
                    .where(
                        TelegramConversation.id == claim.conversation_id,
                        TelegramConversation.user_id == claim.user_id,
                        TelegramConversation.workspace_id == claim.workspace_id,
                    )
                    .with_for_update()
                )
                if conversation is None:
                    raise ConversationScopeError("conversation is unavailable")
                notification = await self._notifications.create_in_session(
                    session,
                    event_id=claim.event_id,
                    user_id=claim.user_id,
                    workspace_id=claim.workspace_id,
                    situation_id=conversation.situation_id,
                    agent_run_id=None,
                    kind=NotificationKind.REACTIVE,
                    urgency=NotificationUrgency.LOW,
                    message=response_text,
                    dedupe_key=f"conversation-turn:{claim.turn_id}:{agent_version}",
                    destination=self._notification_destination,
                    created_at=completed_at,
                )
                assistant_turn = ConversationTurn(
                    id=uuid5(_TURN_NAMESPACE, f"notification:{notification.id}"),
                    user_id=claim.user_id,
                    workspace_id=claim.workspace_id,
                    conversation_id=conversation.id,
                    role=ConversationTurnRole.ASSISTANT,
                    status=ConversationTurnStatus.SUCCEEDED,
                    sequence=conversation.next_sequence,
                    text=response_text,
                    event_id=None,
                    notification_id=notification.id,
                    agent_version=agent_version,
                    attempt_count=1,
                    provider_response_id=provider_response_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    tool_audit=[item.model_dump(mode="json") for item in tool_audit],
                    completed_at=completed_at,
                )
                conversation.next_sequence += 1
                conversation.last_activity_at = completed_at
                user_turn.status = ConversationTurnStatus.SUCCEEDED
                user_turn.claim_id = None
                user_turn.lease_expires_at = None
                user_turn.completed_at = completed_at
                user_turn.failure_code = None
                user_turn.failure_summary = None
                session.add(assistant_turn)
                await session.execute(
                    update(EventProcessing)
                    .where(EventProcessing.event_id == claim.event_id)
                    .values(stage=ProcessingStage.HANDLED, processed_at=completed_at)
                )
                await session.flush()
                return _turn_record(assistant_turn)

    async def fail(
        self,
        claim: ConversationTurnClaim,
        *,
        retryable: bool,
        max_attempts: int,
        next_retry_at: datetime | None,
        failure_code: str,
        failure_summary: str,
        completed_at: datetime,
    ) -> ConversationTurnRecord:
        should_retry = retryable and claim.attempt_count < max_attempts
        status = (
            ConversationTurnStatus.RETRYABLE_FAILURE
            if should_retry
            else ConversationTurnStatus.PERMANENT_FAILURE
        )
        statement = (
            update(ConversationTurn)
            .where(_claim_predicate(claim))
            .values(
                status=status,
                next_retry_at=next_retry_at if should_retry else None,
                claim_id=None,
                lease_expires_at=None,
                failure_code=failure_code,
                failure_summary=failure_summary,
                completed_at=completed_at,
            )
            .returning(ConversationTurn)
        )
        async with self._database.session() as session:
            async with session.begin():
                row = (await session.scalars(statement)).one_or_none()
                if row is None:
                    raise ConversationConflictError("conversation claim is stale")
                if not should_retry:
                    await session.execute(
                        update(EventProcessing)
                        .where(EventProcessing.event_id == claim.event_id)
                        .values(stage=ProcessingStage.HANDLED, processed_at=completed_at)
                    )
                return _turn_record(row)

    async def get_turn(
        self, *, turn_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> ConversationTurnRecord | None:
        statement = select(ConversationTurn).where(
            ConversationTurn.id == turn_id,
            ConversationTurn.user_id == user_id,
            ConversationTurn.workspace_id == workspace_id,
        )
        async with self._database.session() as session:
            row = await session.scalar(statement)
        return None if row is None else _turn_record(row)

    async def find_turn_for_event(
        self, *, event_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> ConversationTurnRecord | None:
        statement = select(ConversationTurn).where(
            ConversationTurn.event_id == event_id,
            ConversationTurn.user_id == user_id,
            ConversationTurn.workspace_id == workspace_id,
        )
        async with self._database.session() as session:
            row = await session.scalar(statement)
        return None if row is None else _turn_record(row)

    async def _resolve_conversation(
        self,
        session: AsyncSession,
        *,
        event: Event,
        account: TelegramAccount,
        now: datetime,
    ) -> TelegramConversation:
        command = event.payload.get("command")
        reply_to = event.payload.get("reply_to_message_id")
        if command == "new":
            await session.execute(
                update(TelegramConversation)
                .where(
                    TelegramConversation.telegram_account_id == account.id,
                    TelegramConversation.kind == ConversationKind.GENERAL,
                    TelegramConversation.status == ConversationStatus.ACTIVE,
                )
                .values(status=ConversationStatus.CLOSED)
            )
            return await self._create_general(session, event=event, account=account, now=now)

        if isinstance(reply_to, int):
            notification = await session.scalar(
                select(Notification).where(
                    Notification.telegram_account_id == account.id,
                    Notification.provider_message_id == reply_to,
                    Notification.user_id == event.user_id,
                    Notification.workspace_id == event.workspace_id,
                    Notification.situation_id.is_not(None),
                )
            )
            if notification is not None and notification.situation_id is not None:
                conversation = await self._get_or_create_for_situation(
                    session,
                    event=event,
                    account=account,
                    situation_id=notification.situation_id,
                    now=now,
                )
                await self._ensure_notification_turn(
                    session, conversation=conversation, notification=notification, now=now
                )
                return conversation

        active_conversation = await session.scalar(
            select(TelegramConversation)
            .where(
                TelegramConversation.telegram_account_id == account.id,
                TelegramConversation.user_id == event.user_id,
                TelegramConversation.workspace_id == event.workspace_id,
                TelegramConversation.status == ConversationStatus.ACTIVE,
            )
            .order_by(TelegramConversation.last_activity_at.desc())
            .limit(1)
        )
        if active_conversation is not None:
            return active_conversation
        return await self._create_general(session, event=event, account=account, now=now)

    async def _create_general(
        self, session: AsyncSession, *, event: Event, account: TelegramAccount, now: datetime
    ) -> TelegramConversation:
        situation_id = uuid5(_GENERAL_SITUATION_NAMESPACE, str(event.id))
        conversation_id = uuid5(_CONVERSATION_NAMESPACE, f"general:{situation_id}")
        situation = Situation(
            id=situation_id,
            user_id=event.user_id,
            workspace_id=event.workspace_id,
            type=SituationType.TELEGRAM_CHAT,
            title="Telegram conversation",
            lifecycle=SituationLifecycle.ACTIVE,
            attention=AttentionLevel.NORMAL,
            summary="",
            current_state="CONVERSING",
            next_action=None,
            next_expected=None,
            version=1,
            last_activity_at=now,
        )
        session.add(situation)
        # ORM models intentionally have no navigation relationships, so make the parent insert
        # visible before inserting composite-FK conversation and correlation children.
        await session.flush()
        conversation = TelegramConversation(
            id=conversation_id,
            user_id=event.user_id,
            workspace_id=event.workspace_id,
            telegram_account_id=account.id,
            situation_id=situation_id,
            kind=ConversationKind.GENERAL,
            status=ConversationStatus.ACTIVE,
            summary="",
            next_sequence=1,
            last_activity_at=now,
        )
        session.add(conversation)
        session.add(
            SituationCorrelationKey(
                workspace_id=event.workspace_id,
                user_id=event.user_id,
                situation_id=situation_id,
                correlation_key=f"telegram-conversation:{conversation_id}",
                kind=CorrelationKeyKind.TELEGRAM_CONVERSATION,
                created_at=now,
            )
        )
        await session.flush()
        return conversation

    async def _get_or_create_for_situation(
        self,
        session: AsyncSession,
        *,
        event: Event,
        account: TelegramAccount,
        situation_id: UUID,
        now: datetime,
    ) -> TelegramConversation:
        existing = await session.scalar(
            select(TelegramConversation).where(
                TelegramConversation.telegram_account_id == account.id,
                TelegramConversation.situation_id == situation_id,
            )
        )
        if existing is not None:
            existing.status = ConversationStatus.ACTIVE
            existing.last_activity_at = now
            return existing
        conversation = TelegramConversation(
            id=uuid5(_CONVERSATION_NAMESPACE, f"situation:{account.id}:{situation_id}"),
            user_id=event.user_id,
            workspace_id=event.workspace_id,
            telegram_account_id=account.id,
            situation_id=situation_id,
            kind=ConversationKind.SITUATION,
            status=ConversationStatus.ACTIVE,
            summary="",
            next_sequence=1,
            last_activity_at=now,
        )
        session.add(conversation)
        await session.flush()
        return conversation

    async def _ensure_notification_turn(
        self,
        session: AsyncSession,
        *,
        conversation: TelegramConversation,
        notification: Notification,
        now: datetime,
    ) -> None:
        exists = await session.scalar(
            select(ConversationTurn.id).where(ConversationTurn.notification_id == notification.id)
        )
        if exists is not None:
            return
        locked_conversation = await session.scalar(
            select(TelegramConversation)
            .where(TelegramConversation.id == conversation.id)
            .with_for_update()
        )
        if locked_conversation is None:
            raise ConversationConflictError("conversation was not visible")
        session.add(
            ConversationTurn(
                id=uuid5(_TURN_NAMESPACE, f"notification:{notification.id}"),
                user_id=notification.user_id,
                workspace_id=notification.workspace_id,
                conversation_id=locked_conversation.id,
                role=ConversationTurnRole.ASSISTANT,
                status=ConversationTurnStatus.SUCCEEDED,
                sequence=locked_conversation.next_sequence,
                text=notification.message,
                event_id=None,
                notification_id=notification.id,
                agent_version=None,
                attempt_count=1,
                completed_at=notification.sent_at or now,
            )
        )
        locked_conversation.next_sequence += 1
        await session.flush()


def _claim_predicate(claim: ConversationTurnClaim) -> Any:
    return and_(
        ConversationTurn.id == claim.turn_id,
        ConversationTurn.conversation_id == claim.conversation_id,
        ConversationTurn.user_id == claim.user_id,
        ConversationTurn.workspace_id == claim.workspace_id,
        ConversationTurn.status == ConversationTurnStatus.RUNNING,
        ConversationTurn.claim_id == claim.claim_id,
    )


def _event_text(event: Event) -> str:
    value = event.payload.get("text")
    if not isinstance(value, str) or not value.strip():
        raise ConversationScopeError("Telegram message text is unavailable")
    return value.strip()[:4000]


def _bounded_history(
    rows: tuple[ConversationTurn, ...], max_chars: int
) -> tuple[ConversationHistoryTurn, ...]:
    selected: list[ConversationHistoryTurn] = []
    remaining = max_chars
    for row in reversed(rows):
        if len(row.text) > remaining:
            continue
        selected.append(ConversationHistoryTurn(role=ConversationTurnRole(row.role), text=row.text))
        remaining -= len(row.text)
    return tuple(reversed(selected))


def _conversation_record(row: TelegramConversation) -> ConversationRecord:
    return ConversationRecord(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        telegram_account_id=row.telegram_account_id,
        situation_id=row.situation_id,
        kind=ConversationKind(row.kind),
        status=ConversationStatus(row.status),
        summary=row.summary,
        next_sequence=row.next_sequence,
        last_activity_at=row.last_activity_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _turn_record(row: ConversationTurn) -> ConversationTurnRecord:
    return ConversationTurnRecord(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        conversation_id=row.conversation_id,
        role=ConversationTurnRole(row.role),
        status=ConversationTurnStatus(row.status),
        sequence=row.sequence,
        text=row.text,
        event_id=row.event_id,
        notification_id=row.notification_id,
        agent_version=row.agent_version,
        attempt_count=row.attempt_count,
        next_retry_at=row.next_retry_at,
        claim_id=row.claim_id,
        lease_expires_at=row.lease_expires_at,
        provider_response_id=row.provider_response_id,
        usage=AgentUsage(
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_tokens=row.total_tokens,
        ),
        tool_audit=tuple(ToolCallAudit.model_validate(value) for value in row.tool_audit),
        failure_code=row.failure_code,
        failure_summary=row.failure_summary,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )
