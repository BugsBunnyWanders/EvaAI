from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest
from sqlalchemy import func, select

from eva_ai.agent.errors import AgentConflictError
from eva_ai.agent.repository import AgentRunRepository
from eva_ai.agent.types import (
    AgentDecision,
    AgentInvestigationResult,
    AgentRunRequestedMessage,
    AgentRunStatus,
    AgentUsage,
    NotificationProposal,
    NotificationUrgency,
)
from eva_ai.db import Database
from eva_ai.db.models import Notification, OutboxMessage, Situation
from eva_ai.events.service import EventService
from eva_ai.events.types import NewEvent, PrincipalType
from eva_ai.relevance.repository import RelevanceRepository
from eva_ai.relevance.types import (
    AIRelevancePayload,
    ClassifierResult,
    EvaluationAttemptStatus,
    EvaluationTrigger,
    FinishEvaluationAttempt,
    RelevanceCategory,
    RelevanceDisposition,
    SignalDraft,
    SignalProducer,
    StartEvaluationAttempt,
)
from eva_ai.situations.types import AttentionLevel, SituationLifecycle, SituationType
from tests.integration.factories import create_scope

NOW = datetime(2026, 9, 7, tzinfo=UTC)


@pytest.mark.integration
async def test_schedule_claim_complete_is_scoped_and_idempotent(database: Database) -> None:
    scope = await create_scope(database)
    event_id = (
        await EventService(database, "eva-events").ingest(
            NewEvent(
                user_id=scope.user_id,
                workspace_id=scope.workspace_id,
                source="gmail",
                event_type="email.received",
                external_id="message-1",
                idempotency_key=f"agent-test:{uuid7()}",
                occurred_at=NOW,
                principal_type=PrincipalType.EXTERNAL,
                payload={"message_id": "message-1", "thread_id": "thread-1"},
                correlation_keys=["gmail-thread:thread-1"],
            )
        )
    ).event_id
    situation_id = uuid7()
    async with database.session() as session:
        async with session.begin():
            session.add(
                Situation(
                    id=situation_id,
                    user_id=scope.user_id,
                    workspace_id=scope.workspace_id,
                    type=SituationType.EMAIL_THREAD,
                    title="Meeting",
                    lifecycle=SituationLifecycle.OPEN,
                    attention=AttentionLevel.HIGH,
                    summary="A meeting needs investigation.",
                    current_state="NEW",
                    version=1,
                    last_activity_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    result = ClassifierResult(
        relevance=0.9,
        importance=0.8,
        urgency=0.7,
        confidence=0.9,
        category=RelevanceCategory.REQUEST_OR_COMMITMENT,
        recommended_action=RelevanceDisposition.INVESTIGATE,
        reason="The user must choose a time.",
    )
    relevance = RelevanceRepository(database)
    evaluation_key = uuid7()
    attempt = await relevance.start_attempt(
        StartEvaluationAttempt(
            event_id=event_id,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            evaluation_key=evaluation_key,
            attempt_number=1,
            trigger=EvaluationTrigger.INITIAL,
            provider="openai",
            model="gpt-test",
            classifier_version="v1",
            input_digest="a" * 64,
            started_at=NOW,
        )
    )
    await relevance.finish_attempt(
        FinishEvaluationAttempt(
            attempt_id=attempt.id,
            event_id=event_id,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            evaluation_key=evaluation_key,
            status=EvaluationAttemptStatus.SUCCEEDED,
            completed_at=NOW,
        )
    )
    signal = await relevance.persist_signal(
        SignalDraft(
            event_id=event_id,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            payload=AIRelevancePayload(result=result, disposition=RelevanceDisposition.INVESTIGATE),
            confidence=0.9,
            disposition=RelevanceDisposition.INVESTIGATE,
            producer=SignalProducer.AI,
            provider="openai",
            model="gpt-test",
            classifier_version="v1",
            policy_version="p1",
            input_digest="a" * 64,
            trigger=EvaluationTrigger.INITIAL,
            evaluation_key=evaluation_key,
            successful_attempt_id=attempt.id,
            created_at=NOW,
        ),
        situation_id=situation_id,
    )
    runs = AgentRunRepository(database)

    async with database.session() as session:
        async with session.begin():
            first = await runs.schedule_in_session(
                session,
                event_id=event_id,
                signal_id=signal.id,
                situation_id=situation_id,
                user_id=scope.user_id,
                workspace_id=scope.workspace_id,
                destination="eva-agent-runs",
                provider="openai",
                model="gpt-5.6-sol",
                agent_version="v1",
                prompt_version="p1",
                queued_at=NOW,
            )
    async with database.session() as session:
        async with session.begin():
            replay = await runs.schedule_in_session(
                session,
                event_id=event_id,
                signal_id=signal.id,
                situation_id=situation_id,
                user_id=scope.user_id,
                workspace_id=scope.workspace_id,
                destination="eva-agent-runs",
                provider="openai",
                model="gpt-5.6-sol",
                agent_version="v1",
                prompt_version="p1",
                queued_at=NOW,
            )
    assert replay.id == first.id

    async with database.session() as session:
        outbox = await session.scalar(
            select(OutboxMessage).where(
                OutboxMessage.message_type == "agent.run.requested",
                OutboxMessage.event_id == event_id,
            )
        )
        count = await session.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(
                OutboxMessage.message_type == "agent.run.requested",
                OutboxMessage.event_id == event_id,
            )
        )
    assert outbox is not None and count == 1
    message = AgentRunRequestedMessage.model_validate(outbox.payload)
    wrong_scope = message.model_copy(update={"workspace_id": uuid7()})
    assert await runs.claim(wrong_scope, now=NOW, lease_seconds=60) is None

    first_claim = await runs.claim(message, now=NOW, lease_seconds=60)
    assert first_claim is not None and first_claim.attempt_count == 1
    claim = await runs.claim(message, now=NOW + timedelta(seconds=61), lease_seconds=60)
    assert claim is not None and claim.attempt_count == 2
    with pytest.raises(AgentConflictError, match="claim is stale"):
        await runs.complete(
            first_claim,
            result=AgentInvestigationResult(
                decision=AgentDecision.NO_ACTION,
                reasoning_summary="No user action is required.",
            ),
            input_digest="b" * 64,
            provider_response_id="stale-response",
            usage=AgentUsage(),
            tool_audit=(),
            completed_at=NOW,
        )
    completed = await runs.complete(
        claim,
        result=AgentInvestigationResult(
            decision=AgentDecision.NO_ACTION,
            reasoning_summary="No user action is required.",
        ),
        input_digest="b" * 64,
        provider_response_id="response-1",
        usage=AgentUsage(input_tokens=10, output_tokens=3, total_tokens=13),
        tool_audit=(),
        completed_at=NOW + timedelta(seconds=61),
    )

    assert completed.status is AgentRunStatus.SUCCEEDED
    assert completed.result is not None
    assert await runs.claim(message, now=NOW, lease_seconds=60) is None

    # A new agent version is a distinct logical run. Its user-facing result must commit the
    # proactive Notification and delivery outbox intent with the successful run.
    proactive_runs = AgentRunRepository(database, "eva-telegram-delivery")
    async with database.session() as session:
        async with session.begin():
            proactive = await proactive_runs.schedule_in_session(
                session,
                event_id=event_id,
                signal_id=signal.id,
                situation_id=situation_id,
                user_id=scope.user_id,
                workspace_id=scope.workspace_id,
                destination="eva-agent-runs",
                provider="openai",
                model="gpt-5.6-sol",
                agent_version="v2",
                prompt_version="p2",
                queued_at=NOW + timedelta(minutes=2),
            )
    proactive_message = AgentRunRequestedMessage(
        outbox_message_id=uuid7(),
        agent_run_id=proactive.id,
        event_id=event_id,
        signal_id=signal.id,
        situation_id=situation_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
    )
    proactive_claim = await proactive_runs.claim(
        proactive_message, now=NOW + timedelta(minutes=2), lease_seconds=60
    )
    assert proactive_claim is not None
    await proactive_runs.complete(
        proactive_claim,
        result=AgentInvestigationResult(
            decision=AgentDecision.NOTIFY_USER,
            reasoning_summary="The user needs the meeting update.",
            notification=NotificationProposal(
                urgency=NotificationUrgency.HIGH,
                message="The meeting time changed. Would you like the details?",
            ),
        ),
        input_digest="c" * 64,
        provider_response_id="response-2",
        usage=AgentUsage(),
        tool_audit=(),
        completed_at=NOW + timedelta(minutes=3),
    )

    async with database.session() as session:
        notification = await session.scalar(
            select(Notification).where(Notification.agent_run_id == proactive.id)
        )
        delivery_count = await session.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(
                OutboxMessage.message_type == "notification.delivery.requested",
                OutboxMessage.event_id == event_id,
            )
        )
    assert notification is not None
    assert notification.message == "The meeting time changed. Would you like the details?"
    assert delivery_count == 1
