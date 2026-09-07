from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid7

import pytest
from sqlalchemy import func, select

from eva_ai.agent.repository import AgentRunRepository
from eva_ai.db import Database
from eva_ai.db.models import AgentRun, OutboxMessage, RelevanceEvaluationAttempt, Signal, Situation
from eva_ai.events import EventAvailableMessage, EventProcessor, EventService
from eva_ai.events.types import NewEvent, PrincipalType
from eva_ai.relevance.classifier import RelevanceClassifierRunner, ScriptedRelevanceClassifier
from eva_ai.relevance.context import ContextBounds, RelevanceContextBuilder
from eva_ai.relevance.filters import RelevanceRuleSet, StaticRelevanceRuleProvider
from eva_ai.relevance.policy import RelevanceRoutingPolicy, RoutingThresholds
from eva_ai.relevance.repository import RelevanceRepository
from eva_ai.relevance.service import RelevanceEventHandler, RelevanceService
from eva_ai.relevance.types import (
    ClassifierResult,
    ReevaluateEvent,
    RelevanceCategory,
    RelevanceDisposition,
)
from eva_ai.situations.repository import SituationRepository
from tests.integration.factories import Scope, create_scope

NOW = datetime(2026, 9, 1, tzinfo=UTC)


async def ingest(
    database: Database, scope: Scope, *, sender: str = "Eva <eva@example.com>"
) -> EventAvailableMessage:
    marker = str(uuid7())
    result = await EventService(database, "eva-events").ingest(
        NewEvent(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            source="gmail",
            event_type="email.received",
            external_id=marker,
            idempotency_key=f"gmail:{marker}",
            occurred_at=NOW,
            principal_type=PrincipalType.EXTERNAL,
            payload={
                "message_id": marker,
                "thread_id": "thread-1",
                "headers": {"from": sender, "subject": "Flight changed"},
                "snippet": "Your departure moved.",
                "label_ids": ["INBOX"],
                "plain_text": "The flight now leaves at 10:00.",
            },
            correlation_keys=["gmail-thread:thread-1"],
        )
    )
    return EventAvailableMessage(
        outbox_message_id=uuid7(),
        event_id=result.event_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        event_type="email.received",
        schema_version=1,
    )


def classifier_result(action: RelevanceDisposition) -> ClassifierResult:
    high = action is RelevanceDisposition.NOTIFY
    investigate = action is RelevanceDisposition.INVESTIGATE
    return ClassifierResult(
        relevance=0.9 if high else (0.7 if investigate else 0.4),
        importance=0.8 if high else 0.4,
        urgency=0.7 if high else 0.3,
        confidence=0.9,
        category=RelevanceCategory.TRAVEL,
        recommended_action=action,
        reason="Travel update",
    )


def handler(
    database: Database,
    script: tuple[ClassifierResult | BaseException, ...],
    *,
    rules: RelevanceRuleSet | None = None,
    schedule_agent: bool = False,
) -> tuple[RelevanceEventHandler, RelevanceRepository]:
    repository = RelevanceRepository(database)
    runner = RelevanceClassifierRunner(
        classifier=ScriptedRelevanceClassifier(script),
        attempts=repository,
        provider="openai",
        model="gpt-test",
        classifier_version="v1",
        max_attempts=3,
        initial_backoff_seconds=0.001,
        max_backoff_seconds=0.001,
        jitter_ratio=0,
        clock=lambda: NOW,
    )
    policy = RelevanceRoutingPolicy(RoutingThresholds(0.75, 0.7, 0.65, 0.6, 0.65, 0.2, 0.8), "p1")
    return (
        RelevanceEventHandler(
            signals=repository,
            situations=SituationRepository(database),
            rules=StaticRelevanceRuleProvider(rules or RelevanceRuleSet()),
            context_builder=RelevanceContextBuilder(repository, ContextBounds()),
            classifier=runner,
            policy=policy,
            classifier_version="v1",
            agent_runs=AgentRunRepository(database) if schedule_agent else None,
            agent_destination="eva-agent-runs",
            agent_model="gpt-5.6-sol",
            agent_version="investigation-v1",
            agent_prompt_version="investigation-prompt-v1",
            clock=lambda: NOW,
        ),
        repository,
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("action", "expected_situations", "expected_agent_runs"),
    [
        (RelevanceDisposition.RECORD, 0, 0),
        (RelevanceDisposition.NOTIFY, 1, 1),
    ],
)
async def test_ai_routes_signal_and_only_actionable_mail_to_situation(
    database: Database,
    action: RelevanceDisposition,
    expected_situations: int,
    expected_agent_runs: int,
) -> None:
    scope = await create_scope(database)
    message = await ingest(database, scope)
    relevance_handler, repository = handler(
        database, (classifier_result(action),), schedule_agent=True
    )

    await EventProcessor(database, 300).process(message, relevance_handler, NOW)

    current = await repository.get_current(
        event_id=message.event_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
    )
    assert current is not None
    assert current.disposition is action
    assert (current.situation_id is not None) is bool(expected_situations)
    assert await count(database, Situation, scope) == expected_situations
    assert await count(database, Signal, scope) == 1
    assert await count(database, RelevanceEvaluationAttempt, scope) == 1
    assert await count(database, AgentRun, scope) == expected_agent_runs


@pytest.mark.integration
async def test_deterministic_ignore_skips_classifier_and_situation(database: Database) -> None:
    scope = await create_scope(database)
    message = await ingest(database, scope, sender="Ignored <ignored@example.com>")
    relevance_handler, repository = handler(
        database, (), rules=RelevanceRuleSet(ignored_senders=("ignored@example.com",))
    )

    await EventProcessor(database, 300).process(message, relevance_handler, NOW)

    current = await repository.get_current(
        event_id=message.event_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
    )
    assert current is not None
    assert current.disposition is RelevanceDisposition.IGNORE
    assert await count(database, RelevanceEvaluationAttempt, scope) == 0
    assert await count(database, Situation, scope) == 0


@pytest.mark.integration
async def test_investigate_transactionally_schedules_one_agent_run(database: Database) -> None:
    scope = await create_scope(database)
    message = await ingest(database, scope)
    relevance_handler, repository = handler(
        database,
        (classifier_result(RelevanceDisposition.INVESTIGATE),),
        schedule_agent=True,
    )

    await EventProcessor(database, 300).process(message, relevance_handler, NOW)

    current = await repository.get_current(
        event_id=message.event_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
    )
    assert current is not None and current.disposition is RelevanceDisposition.INVESTIGATE
    assert await count(database, AgentRun, scope) == 1
    async with database.session() as session:
        agent_messages = await session.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(
                OutboxMessage.event_id == message.event_id,
                OutboxMessage.message_type == "agent.run.requested",
            )
        )
    assert agent_messages == 1


@pytest.mark.integration
async def test_explicit_reevaluation_supersedes_without_erasing_history(database: Database) -> None:
    scope = await create_scope(database)
    message = await ingest(database, scope)
    relevance_handler, repository = handler(
        database,
        (
            classifier_result(RelevanceDisposition.RECORD),
            classifier_result(RelevanceDisposition.NOTIFY),
        ),
    )
    await EventProcessor(database, 300).process(message, relevance_handler, NOW)
    first = await repository.get_current(
        event_id=message.event_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
    )
    assert first is not None

    second = await RelevanceService(database, repository, relevance_handler).reevaluate(
        ReevaluateEvent(
            event_id=message.event_id,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            evaluation_key=uuid7(),
            reason="Travel is now urgent",
            requested_at=NOW + timedelta(minutes=1),
        )
    )

    assert second.disposition is RelevanceDisposition.NOTIFY
    assert second.supersedes_signal_id == first.id
    assert second.operator_reason == "Travel is now urgent"
    history = await repository.history(
        event_id=message.event_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
    )
    assert [item.id for item in history] == [first.id, second.id]
    assert [item.is_current for item in history] == [False, True]


@pytest.mark.integration
async def test_backfill_processes_one_bounded_selected_batch(database: Database) -> None:
    scope = await create_scope(database)
    first = await ingest(database, scope)
    second = await ingest(database, scope)
    relevance_handler, repository = handler(
        database,
        (
            classifier_result(RelevanceDisposition.RECORD),
            classifier_result(RelevanceDisposition.RECORD),
        ),
    )

    summary = await RelevanceService(database, repository, relevance_handler).backfill(
        user_id=scope.user_id, workspace_id=scope.workspace_id, limit=2
    )

    assert summary.selected == 2
    assert summary.succeeded == 2
    assert summary.failed == 0
    assert len(summary.signal_ids) == 2
    assert (
        await repository.get_current(
            event_id=first.event_id,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
        )
        is not None
    )
    assert (
        await repository.get_current(
            event_id=second.event_id,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
        )
        is not None
    )


async def count(database: Database, model: Any, scope: Scope) -> int:
    async with database.session() as session:
        value = await session.scalar(
            select(func.count())
            .select_from(model)
            .where(
                model.user_id == scope.user_id,
                model.workspace_id == scope.workspace_id,
            )
        )
    return int(value or 0)
