from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest

from eva_ai.db import Database
from eva_ai.db.models import Event, Situation, SituationEvent
from eva_ai.events import PrincipalType
from eva_ai.goals import GoalDraft, GoalMode, GoalRepository, GoalService
from eva_ai.situations import (
    AttentionLevel,
    CorrelationMethod,
    GoalContribution,
    LinkSituationGoal,
    SituationLifecycle,
    SituationNotFoundError,
    SituationRepository,
    SituationScopeError,
    SituationSnapshotUpdate,
    SituationType,
    SituationVersionConflictError,
)
from tests.integration.factories import Scope, create_scope

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


async def create_situation(
    database: Database,
    scope: Scope,
    *,
    title: str,
    lifecycle: SituationLifecycle = SituationLifecycle.OPEN,
    attention: AttentionLevel = AttentionLevel.NORMAL,
    activity: datetime = NOW,
) -> UUID:
    situation_id = uuid7()
    async with database.session() as session:
        async with session.begin():
            session.add(
                Situation(
                    id=situation_id,
                    user_id=scope.user_id,
                    workspace_id=scope.workspace_id,
                    type=SituationType.EMAIL_THREAD,
                    title=title,
                    lifecycle=lifecycle,
                    attention=attention,
                    summary=f"Summary for {title}",
                    current_state="NEW",
                    version=1,
                    last_activity_at=activity,
                    created_at=activity,
                    updated_at=activity,
                )
            )
    return situation_id


@pytest.mark.integration
async def test_repository_gets_only_exact_scope_and_orders_attention_activity_terminal(
    database: Database,
) -> None:
    scope = await create_scope(database)
    other = await create_scope(database)
    repository = SituationRepository(database)
    low_old = await create_situation(
        database, scope, title="Low old", attention=AttentionLevel.LOW, activity=NOW
    )
    low_new = await create_situation(
        database,
        scope,
        title="Low new",
        attention=AttentionLevel.LOW,
        activity=NOW + timedelta(minutes=1),
    )
    urgent = await create_situation(
        database, scope, title="Urgent", attention=AttentionLevel.URGENT
    )
    resolved = await create_situation(
        database,
        scope,
        title="Resolved",
        lifecycle=SituationLifecycle.RESOLVED,
        attention=AttentionLevel.URGENT,
        activity=NOW + timedelta(days=1),
    )

    records = await repository.list(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        limit=10,
    )

    assert [record.id for record in records] == [urgent, low_new, low_old, resolved]
    assert (
        await repository.get(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            situation_id=urgent,
        )
    ) is not None
    assert (
        await repository.get(
            user_id=other.user_id,
            workspace_id=scope.workspace_id,
            situation_id=urgent,
        )
        is None
    )
    assert await repository.list(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        lifecycles=(SituationLifecycle.RESOLVED,),
        limit=10,
    ) == (records[-1],)


@pytest.mark.integration
async def test_snapshot_update_is_conditional_and_increments_exactly_once(
    database: Database,
) -> None:
    scope = await create_scope(database)
    situation_id = await create_situation(database, scope, title="Original")
    repository = SituationRepository(database)
    command = SituationSnapshotUpdate(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=situation_id,
        expected_version=1,
        title="Curated",
        summary="Curated summary",
        current_state="WAITING",
        next_action="Reply",
        next_expected="Response",
        updated_at=NOW + timedelta(hours=1),
    )

    updated = await repository.update_snapshot(command)

    assert updated.version == 2
    assert (updated.title, updated.summary, updated.current_state) == (
        "Curated",
        "Curated summary",
        "WAITING",
    )
    assert updated.lifecycle is SituationLifecycle.OPEN
    assert updated.attention is AttentionLevel.NORMAL
    with pytest.raises(SituationVersionConflictError):
        await repository.update_snapshot(command.model_copy(update={"title": "Stale"}))
    persisted = await repository.get(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=situation_id,
    )
    assert persisted == updated


@pytest.mark.integration
async def test_snapshot_update_hides_wrong_scope_as_not_found(database: Database) -> None:
    scope = await create_scope(database)
    other = await create_scope(database)
    situation_id = await create_situation(database, scope, title="Scoped")
    repository = SituationRepository(database)

    with pytest.raises(SituationNotFoundError):
        await repository.update_snapshot(
            SituationSnapshotUpdate(
                user_id=other.user_id,
                workspace_id=other.workspace_id,
                situation_id=situation_id,
                expected_version=1,
                title="Wrong",
                summary="Wrong",
                current_state="WRONG",
                updated_at=NOW,
            )
        )


@pytest.mark.integration
async def test_explicit_goal_link_upserts_metadata_and_rejects_cross_scope(
    database: Database,
) -> None:
    scope = await create_scope(database)
    other = await create_scope(database)
    situation_id = await create_situation(database, scope, title="Goal context")
    goal_service = GoalService(GoalRepository(database))
    goal = await goal_service.create_explicit(
        GoalDraft(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            title="Deliver project",
            objective="Finish delivery",
            domain="work",
            mode=GoalMode.ACHIEVE,
        )
    )
    repository = SituationRepository(database)
    original = LinkSituationGoal(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=situation_id,
        goal_id=goal.id,
        relevance=Decimal("0.500"),
        contribution=GoalContribution.CONTEXT,
        reasoning=None,
        linked_at=NOW,
    )

    await repository.link_goal(original)
    updated = await repository.link_goal(
        original.model_copy(
            update={
                "relevance": Decimal("0.900"),
                "contribution": GoalContribution.SUPPORTS,
                "reasoning": "Directly advances delivery",
                "linked_at": NOW + timedelta(minutes=1),
            }
        )
    )

    assert updated.relevance == Decimal("0.900")
    assert updated.contribution is GoalContribution.SUPPORTS
    assert await repository.list_goals(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=situation_id,
    ) == (updated,)
    with pytest.raises(SituationScopeError):
        await repository.link_goal(original.model_copy(update={"user_id": other.user_id}))


@pytest.mark.integration
async def test_event_projection_exposes_metadata_without_payload(database: Database) -> None:
    scope = await create_scope(database)
    situation_id = await create_situation(database, scope, title="Mail")
    event_id = uuid7()
    async with database.session() as session:
        async with session.begin():
            session.add(
                Event(
                    id=event_id,
                    user_id=scope.user_id,
                    workspace_id=scope.workspace_id,
                    source="gmail",
                    event_type="email.received",
                    idempotency_key=f"test:{event_id}",
                    occurred_at=NOW,
                    received_at=NOW,
                    principal_type=PrincipalType.EXTERNAL,
                    payload={"secret_body": "must not be projected"},
                    event_metadata={},
                    correlation_keys=["gmail-thread:thread-1"],
                    schema_version=1,
                )
            )
    async with database.session() as session:
        async with session.begin():
            session.add(
                SituationEvent(
                    situation_id=situation_id,
                    event_id=event_id,
                    user_id=scope.user_id,
                    workspace_id=scope.workspace_id,
                    correlation_method=CorrelationMethod.DETERMINISTIC_KEY,
                    correlation_key="gmail-thread:thread-1",
                    linked_at=NOW,
                )
            )

    records = await SituationRepository(database).list_events(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=situation_id,
    )

    assert len(records) == 1
    assert records[0].event_id == event_id
    assert records[0].event_occurred_at == NOW
    assert "payload" not in type(records[0]).model_fields
