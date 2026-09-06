from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest

from eva_ai.db import Database
from eva_ai.db.models import Situation, SituationGoal
from eva_ai.goals import GoalDraft, GoalMode, GoalRepository, GoalSource, GoalStatus
from eva_ai.memory.embedding import EmbeddedText
from eva_ai.memory.errors import MemoryScopeError
from eva_ai.memory.repository import MemoryRepository
from eva_ai.memory.types import (
    EpisodicMemoryDraft,
    EpisodicMemoryStatus,
    MemoryEpisodeType,
    MemoryFactDraft,
    MemoryFactStatus,
    MemoryScopeType,
    MemorySourceType,
)
from eva_ai.situations import (
    AttentionLevel,
    GoalContribution,
    SituationLifecycle,
    SituationType,
)
from tests.integration.factories import Scope, create_scope

NOW = datetime(2026, 9, 6, tzinfo=UTC)


def fact(scope: Scope, *, value: str, key: str, valid_from: datetime) -> MemoryFactDraft:
    return MemoryFactDraft(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        namespace="career.preferences",
        key="interview_time",
        value_json={"period": value},
        scope_type=MemoryScopeType.WORKSPACE,
        scope_id=scope.workspace_id,
        source_type=MemorySourceType.USER_EXPLICIT,
        source_ref="operator:test",
        confidence=Decimal("1"),
        idempotency_key=key,
        valid_from=valid_from,
    )


def episode(scope: Scope, *, key: str, summary: str) -> EpisodicMemoryDraft:
    return EpisodicMemoryDraft(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        type=MemoryEpisodeType.DECISION,
        summary=summary,
        entities=("acme",),
        importance=Decimal("0.8"),
        confidence=Decimal("1"),
        source_type=MemorySourceType.USER_EXPLICIT,
        source_ref="operator:test",
        idempotency_key=key,
        occurred_at=NOW,
    )


def embedded(first: float, second: float) -> EmbeddedText:
    values = [0.0] * 1536
    values[0] = first
    values[1] = second
    return EmbeddedText(
        values=tuple(values),
        model="text-embedding-3-small",
        dimensions=1536,
        input_digest="a" * 64,
        embedded_at=NOW,
    )


@pytest.mark.integration
async def test_fact_put_is_idempotent_and_supersedes_in_one_slot(database: Database) -> None:
    scope = await create_scope(database)
    repository = MemoryRepository(database)
    first_draft = fact(scope, value="afternoon", key="fact:v1", valid_from=NOW)

    first = await repository.put_fact(first_draft)
    replay = await repository.put_fact(first_draft.model_copy(update={"id": UUID(int=9)}))
    second = await repository.put_fact(
        fact(scope, value="morning", key="fact:v2", valid_from=NOW + timedelta(seconds=1))
    )

    assert replay.id == first.id
    assert second.supersedes_memory_id == first.id
    history = await repository.list_facts(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        statuses=(MemoryFactStatus.ACTIVE, MemoryFactStatus.SUPERSEDED),
        limit=10,
    )
    assert [item.status for item in history] == [
        MemoryFactStatus.ACTIVE,
        MemoryFactStatus.SUPERSEDED,
    ]
    assert history[1].valid_until == second.valid_from

    retracted = await repository.retract_fact(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        memory_id=second.id,
        retracted_at=NOW + timedelta(seconds=2),
    )
    assert retracted.status is MemoryFactStatus.RETRACTED


@pytest.mark.integration
async def test_goal_scoped_fact_rejects_cross_workspace_goal(database: Database) -> None:
    scope = await create_scope(database)
    other = await create_scope(database)
    goal = await GoalRepository(database).create(
        GoalDraft(
            user_id=other.user_id,
            workspace_id=other.workspace_id,
            title="Other goal",
            objective="Remain isolated",
            domain="test",
            mode=GoalMode.MAINTAIN,
        ),
        source=GoalSource.USER_EXPLICIT,
        status=GoalStatus.ACTIVE,
        confidence=Decimal("1"),
    )
    invalid = fact(scope, value="afternoon", key="cross-scope", valid_from=NOW).model_copy(
        update={"scope_type": MemoryScopeType.GOAL, "scope_id": goal.id}
    )

    with pytest.raises(MemoryScopeError):
        await MemoryRepository(database).put_fact(invalid)


@pytest.mark.integration
async def test_episode_storage_is_idempotent_retractable_and_vector_search_is_scoped(
    database: Database,
) -> None:
    scope = await create_scope(database)
    other = await create_scope(database)
    repository = MemoryRepository(database)
    nearest_draft = episode(scope, key="episode:nearest", summary="Preferred Acme platform role")
    farther_draft = episode(scope, key="episode:farther", summary="Unrelated travel decision")

    nearest = await repository.create_episode(nearest_draft, embedded(1.0, 0.0))
    replay = await repository.create_episode(
        nearest_draft.model_copy(update={"id": UUID(int=10)}), embedded(1.0, 0.0)
    )
    farther = await repository.create_episode(farther_draft, embedded(0.0, 1.0))
    await repository.create_episode(
        episode(other, key="episode:other", summary="Cross workspace"), embedded(1.0, 0.0)
    )

    assert replay.id == nearest.id
    candidates = await repository.semantic_candidates(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        embedding=embedded(1.0, 0.0),
        limit=10,
    )
    assert [item.memory.id for item in candidates] == [nearest.id, farther.id]
    assert candidates[0].distance < candidates[1].distance

    retracted = await repository.retract_episode(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        memory_id=nearest.id,
    )
    assert retracted.status is EpisodicMemoryStatus.RETRACTED


@pytest.mark.integration
async def test_context_subject_and_fact_order_are_situation_first_and_scoped(
    database: Database,
) -> None:
    scope = await create_scope(database)
    other = await create_scope(database)
    goal = await GoalRepository(database).create(
        GoalDraft(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            title="Choose the best role",
            objective="Select a strong infrastructure opportunity",
            domain="career",
            mode=GoalMode.ACHIEVE,
            priority=90,
        ),
        source=GoalSource.USER_EXPLICIT,
        status=GoalStatus.ACTIVE,
        confidence=Decimal("1"),
    )
    situation_id = uuid7()
    async with database.session() as session:
        async with session.begin():
            session.add(
                Situation(
                    id=situation_id,
                    user_id=scope.user_id,
                    workspace_id=scope.workspace_id,
                    type=SituationType.EMAIL_THREAD,
                    title="Acme interview",
                    lifecycle=SituationLifecycle.ACTIVE,
                    attention=AttentionLevel.HIGH,
                    summary="Recruiter conversation is active",
                    current_state="SCREEN_SCHEDULED",
                    next_action="Prepare questions",
                    next_expected="Interview",
                    version=1,
                    last_activity_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.add(
                SituationGoal(
                    situation_id=situation_id,
                    goal_id=goal.id,
                    user_id=scope.user_id,
                    workspace_id=scope.workspace_id,
                    relevance=Decimal("1"),
                    contribution=GoalContribution.SUPPORTS,
                    reasoning="Directly advances the role search",
                    linked_at=NOW,
                )
            )

    repository = MemoryRepository(database)
    drafts = (
        fact(scope, value="workspace", key="context:workspace", valid_from=NOW).model_copy(
            update={"key": "workspace_preference"}
        ),
        fact(scope, value="goal", key="context:goal", valid_from=NOW).model_copy(
            update={
                "key": "goal_preference",
                "scope_type": MemoryScopeType.GOAL,
                "scope_id": goal.id,
            }
        ),
        fact(scope, value="situation", key="context:situation", valid_from=NOW).model_copy(
            update={
                "key": "situation_preference",
                "scope_type": MemoryScopeType.SITUATION,
                "scope_id": situation_id,
            }
        ),
        fact(scope, value="expired", key="context:expired", valid_from=NOW).model_copy(
            update={
                "key": "expired_preference",
                "valid_until": NOW + timedelta(minutes=1),
            }
        ),
        fact(other, value="other", key="context:other", valid_from=NOW).model_copy(
            update={"key": "other_preference"}
        ),
    )
    for draft in drafts:
        await repository.put_fact(draft)

    subject = await repository.load_context_subject(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=situation_id,
    )
    facts = await repository.list_context_facts(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=situation_id,
        goal_ids=(goal.id,),
        at=NOW + timedelta(minutes=2),
        limit=10,
    )

    assert subject is not None
    assert subject[0].id == situation_id
    assert [item.id for item in subject[1]] == [goal.id]
    assert [item.scope_type for item in facts] == [
        MemoryScopeType.SITUATION,
        MemoryScopeType.GOAL,
        MemoryScopeType.WORKSPACE,
    ]
    assert {item.value_json["period"] for item in facts if isinstance(item.value_json, dict)} == {
        "situation",
        "goal",
        "workspace",
    }
