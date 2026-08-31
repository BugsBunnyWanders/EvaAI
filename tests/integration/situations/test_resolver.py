import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid7

import pytest
from sqlalchemy import func, select

from eva_ai.db import Database
from eva_ai.db.models import (
    Event,
    Situation,
    SituationCorrelationKey,
    SituationEvent,
    SituationGoal,
)
from eva_ai.events import EventService, NewEvent, PrincipalType
from eva_ai.goals import GoalDraft, GoalMode, GoalRepository, GoalService
from eva_ai.situations import (
    AttentionLevel,
    GoalContribution,
    LinkSituationGoal,
    ResolveEvent,
    SituationLifecycle,
    SituationRepository,
    SituationResolution,
    SituationResolver,
    SituationScopeError,
    SituationService,
    SituationSnapshotUpdate,
)
from eva_ai.situations.resolver import InitialSituationSnapshot
from tests.integration.factories import Scope, create_scope

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


async def ingest_gmail_event(
    database: Database,
    scope: Scope,
    *,
    message_id: str,
    thread_id: str,
    occurred_at: datetime,
    subject: str = "Travel update",
    snippet: str = "Your itinerary changed.",
) -> UUID:
    event_id = uuid7()
    await EventService(database, "events").ingest(
        NewEvent(
            id=event_id,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            source="gmail",
            event_type="email.received",
            external_id=message_id,
            idempotency_key=f"gmail:test:{message_id}",
            occurred_at=occurred_at,
            received_at=occurred_at,
            principal_type=PrincipalType.EXTERNAL,
            payload={
                "message_id": message_id,
                "thread_id": thread_id,
                "headers": {"subject": subject},
                "snippet": snippet,
            },
            correlation_keys=[f"gmail-thread:{thread_id}"],
        )
    )
    return event_id


def resolution(scope: Scope, event_id: UUID, *goal_ids: UUID) -> ResolveEvent:
    return ResolveEvent(
        event_id=event_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        goal_ids=goal_ids,
        resolved_at=NOW + timedelta(hours=1),
    )


@pytest.mark.integration
async def test_first_relevant_event_creates_atomic_situation_and_links(database: Database) -> None:
    scope = await create_scope(database)
    goal = await GoalService(GoalRepository(database)).create_explicit(
        GoalDraft(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            title="Manage travel",
            objective="Keep the itinerary current",
            domain="travel",
            mode=GoalMode.MAINTAIN,
        )
    )
    event_id = await ingest_gmail_event(
        database,
        scope,
        message_id="message-1",
        thread_id="thread-1",
        occurred_at=NOW,
        subject="  Re:   Flight change ",
        snippet=" Airline   changed departure. ",
    )
    event_before = await _event_state(database, scope, event_id)

    result = await SituationResolver(SituationRepository(database)).resolve(
        resolution(scope, event_id, goal.id)
    )

    assert result.situation_created is True
    assert result.event_link_created is True
    assert result.linked_goal_ids == (goal.id,)
    assert result.situation.title == "Re: Flight change"
    assert result.situation.summary == "Airline changed departure."
    assert result.situation.last_activity_at == NOW
    assert await _count(database, Situation, scope) == 1
    assert await _count(database, SituationCorrelationKey, scope) == 1
    assert await _count(database, SituationEvent, scope) == 1
    assert await _count(database, SituationGoal, scope) == 1
    assert await _event_state(database, scope, event_id) == event_before


@pytest.mark.integration
async def test_invalid_goal_scope_rolls_back_every_resolution_write(database: Database) -> None:
    scope = await create_scope(database)
    event_id = await ingest_gmail_event(
        database,
        scope,
        message_id="message-invalid-goal",
        thread_id="thread-invalid-goal",
        occurred_at=NOW,
    )

    with pytest.raises(SituationScopeError):
        await SituationResolver(SituationRepository(database)).resolve(
            resolution(scope, event_id, uuid7())
        )

    assert await _count(database, Situation, scope) == 0
    assert await _count(database, SituationCorrelationKey, scope) == 0
    assert await _count(database, SituationEvent, scope) == 0
    assert await _count(database, SituationGoal, scope) == 0


@pytest.mark.integration
async def test_repeat_and_later_thread_events_reuse_without_overwriting_snapshot(
    database: Database,
) -> None:
    scope = await create_scope(database)
    repository = SituationRepository(database)
    resolver = SituationResolver(repository)
    first_event = await ingest_gmail_event(
        database,
        scope,
        message_id="message-first",
        thread_id="thread-shared",
        occurred_at=NOW,
    )
    first = await resolver.resolve(resolution(scope, first_event))
    repeated = await resolver.resolve(resolution(scope, first_event))
    curated = await repository.update_snapshot(
        SituationSnapshotUpdate(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            situation_id=first.situation.id,
            expected_version=1,
            title="Curated title",
            summary="Curated summary",
            current_state="WAITING_EXTERNAL",
            next_action="Monitor",
            next_expected="Airline response",
            updated_at=NOW + timedelta(minutes=10),
        )
    )
    await SituationService(repository).update_lifecycle(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=first.situation.id,
        lifecycle=SituationLifecycle.WAITING_EXTERNAL,
    )
    await SituationService(repository).update_attention(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=first.situation.id,
        attention=AttentionLevel.HIGH,
    )
    older_event = await ingest_gmail_event(
        database,
        scope,
        message_id="message-older",
        thread_id="thread-shared",
        occurred_at=NOW - timedelta(days=1),
        subject="Must not replace",
        snippet="Must not replace",
    )
    older = await resolver.resolve(resolution(scope, older_event))
    newer_event = await ingest_gmail_event(
        database,
        scope,
        message_id="message-newer",
        thread_id="thread-shared",
        occurred_at=NOW + timedelta(days=1),
        subject="Also must not replace",
        snippet="Also must not replace",
    )
    newer = await resolver.resolve(resolution(scope, newer_event))

    assert repeated.situation_created is False
    assert repeated.event_link_created is False
    assert older.situation.id == first.situation.id == newer.situation.id
    assert older.situation.last_activity_at == NOW
    assert newer.situation.last_activity_at == NOW + timedelta(days=1)
    assert (
        newer.situation.title,
        newer.situation.summary,
        newer.situation.current_state,
        newer.situation.next_action,
        newer.situation.next_expected,
        newer.situation.version,
        newer.situation.lifecycle,
        newer.situation.attention,
    ) == (
        curated.title,
        curated.summary,
        curated.current_state,
        curated.next_action,
        curated.next_expected,
        2,
        SituationLifecycle.WAITING_EXTERNAL,
        AttentionLevel.HIGH,
    )
    assert await _count(database, Situation, scope) == 1
    assert await _count(database, SituationEvent, scope) == 3


@pytest.mark.integration
async def test_different_threads_and_workspaces_remain_separate(database: Database) -> None:
    scope = await create_scope(database)
    other = await create_scope(database)
    resolver = SituationResolver(SituationRepository(database))
    first = await ingest_gmail_event(
        database, scope, message_id="separate-1", thread_id="thread-a", occurred_at=NOW
    )
    second = await ingest_gmail_event(
        database, scope, message_id="separate-2", thread_id="thread-b", occurred_at=NOW
    )
    other_event = await ingest_gmail_event(
        database,
        other,
        message_id="separate-3",
        thread_id="thread-a",
        occurred_at=NOW,
    )

    first_result = await resolver.resolve(resolution(scope, first))
    second_result = await resolver.resolve(resolution(scope, second))
    other_result = await resolver.resolve(resolution(other, other_event))

    assert (
        len({first_result.situation.id, second_result.situation.id, other_result.situation.id}) == 3
    )


@pytest.mark.integration
async def test_resolver_goal_link_is_insert_only_and_reports_only_new_links(
    database: Database,
) -> None:
    scope = await create_scope(database)
    repository = SituationRepository(database)
    goal = await GoalService(GoalRepository(database)).create_explicit(
        GoalDraft(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            title="Travel",
            objective="Handle travel",
            domain="travel",
            mode=GoalMode.MAINTAIN,
        )
    )
    first_event = await ingest_gmail_event(
        database,
        scope,
        message_id="goal-link-1",
        thread_id="goal-link-thread",
        occurred_at=NOW,
    )
    first = await SituationResolver(repository).resolve(resolution(scope, first_event, goal.id))
    await SituationService(repository).link_goal(
        LinkSituationGoal(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            situation_id=first.situation.id,
            goal_id=goal.id,
            relevance=Decimal("0.400"),
            contribution=GoalContribution.BLOCKS,
            reasoning="Curated relationship",
            linked_at=NOW + timedelta(minutes=5),
        )
    )
    later_event = await ingest_gmail_event(
        database,
        scope,
        message_id="goal-link-2",
        thread_id="goal-link-thread",
        occurred_at=NOW + timedelta(minutes=10),
    )

    later = await SituationResolver(repository).resolve(
        resolution(scope, later_event, goal.id, goal.id)
    )
    relationships = await repository.list_goals(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=first.situation.id,
    )

    assert later.linked_goal_ids == ()
    assert len(relationships) == 1
    assert relationships[0].relevance == Decimal("0.400")
    assert relationships[0].contribution is GoalContribution.BLOCKS
    assert relationships[0].reasoning == "Curated relationship"


class BarrierRepository(SituationRepository):
    def __init__(self, database: Database, barrier: asyncio.Barrier) -> None:
        super().__init__(database)
        self._barrier = barrier

    async def resolve_gmail_event(
        self,
        *,
        command: ResolveEvent,
        correlation_key: str,
        initial_snapshot: InitialSituationSnapshot,
    ) -> SituationResolution:
        await self._barrier.wait()
        return await super().resolve_gmail_event(
            command=command,
            correlation_key=correlation_key,
            initial_snapshot=initial_snapshot,
        )


@pytest.mark.integration
async def test_concurrent_first_resolution_converges_without_orphan(database: Database) -> None:
    scope = await create_scope(database)
    first_event = await ingest_gmail_event(
        database,
        scope,
        message_id="concurrent-1",
        thread_id="concurrent-thread",
        occurred_at=NOW,
    )
    second_event = await ingest_gmail_event(
        database,
        scope,
        message_id="concurrent-2",
        thread_id="concurrent-thread",
        occurred_at=NOW + timedelta(minutes=1),
    )
    repository = BarrierRepository(database, asyncio.Barrier(2))
    resolver = SituationResolver(repository)

    first, second = await asyncio.gather(
        resolver.resolve(resolution(scope, first_event)),
        resolver.resolve(resolution(scope, second_event)),
    )

    assert first.situation.id == second.situation.id
    assert sum(result.situation_created for result in (first, second)) == 1
    assert await _count(database, Situation, scope) == 1
    assert await _count(database, SituationCorrelationKey, scope) == 1
    assert await _count(database, SituationEvent, scope) == 2


@pytest.mark.integration
async def test_ingestion_without_resolution_creates_no_situation(database: Database) -> None:
    scope = await create_scope(database)
    await ingest_gmail_event(
        database,
        scope,
        message_id="ingestion-only",
        thread_id="ingestion-only-thread",
        occurred_at=NOW,
    )

    assert await _count(database, Situation, scope) == 0
    assert await _count(database, SituationCorrelationKey, scope) == 0


async def _count(database: Database, model: Any, scope: Scope) -> int:
    workspace_id = model.workspace_id
    user_id = model.user_id
    async with database.session() as session:
        value = await session.scalar(
            select(func.count())
            .select_from(model)
            .where(
                workspace_id == scope.workspace_id,
                user_id == scope.user_id,
            )
        )
    return int(value or 0)


async def _event_state(database: Database, scope: Scope, event_id: UUID) -> dict[str, object]:
    async with database.session() as session:
        row = (
            (
                await session.execute(
                    select(
                        Event.source,
                        Event.event_type,
                        Event.external_id,
                        Event.occurred_at,
                        Event.received_at,
                        Event.payload,
                        Event.event_metadata,
                        Event.correlation_keys,
                        Event.schema_version,
                    ).where(
                        Event.id == event_id,
                        Event.user_id == scope.user_id,
                        Event.workspace_id == scope.workspace_id,
                    )
                )
            )
            .mappings()
            .one()
        )
    return dict(row)
