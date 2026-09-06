from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest

from eva_ai.db import Database
from eva_ai.db.models import Goal, Situation, SituationCorrelationKey, SituationGoal
from eva_ai.goals.types import SAFE_AUTONOMY_POLICY, GoalMode, GoalSource, GoalStatus
from eva_ai.relevance.repository import RelevanceRepository
from eva_ai.situations.types import (
    AttentionLevel,
    CorrelationKeyKind,
    GoalContribution,
    SituationLifecycle,
    SituationType,
)
from tests.integration.factories import create_scope

NOW = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.mark.integration
async def test_context_candidates_are_scoped_filtered_and_ranked(database: Database) -> None:
    scope = await create_scope(database)
    other = await create_scope(database)
    active_goal_id, paused_goal_id = uuid7(), uuid7()
    exact_id, goal_id, terminal_id, other_id = uuid7(), uuid7(), uuid7(), uuid7()

    async with database.session() as session:
        async with session.begin():
            session.add_all(
                [
                    _goal(scope.user_id, scope.workspace_id, active_goal_id, GoalStatus.ACTIVE, 90),
                    _goal(
                        scope.user_id, scope.workspace_id, paused_goal_id, GoalStatus.PAUSED, 100
                    ),
                    _situation(
                        scope.user_id, scope.workspace_id, exact_id, AttentionLevel.LOW, NOW
                    ),
                    _situation(
                        scope.user_id,
                        scope.workspace_id,
                        goal_id,
                        AttentionLevel.URGENT,
                        NOW + timedelta(hours=1),
                    ),
                    _situation(
                        scope.user_id,
                        scope.workspace_id,
                        terminal_id,
                        AttentionLevel.URGENT,
                        NOW,
                        SituationLifecycle.RESOLVED,
                    ),
                    _situation(
                        other.user_id, other.workspace_id, other_id, AttentionLevel.URGENT, NOW
                    ),
                ]
            )
            session.add(
                SituationCorrelationKey(
                    workspace_id=scope.workspace_id,
                    user_id=scope.user_id,
                    situation_id=exact_id,
                    correlation_key="gmail-thread:t1",
                    kind=CorrelationKeyKind.GMAIL_THREAD,
                    created_at=NOW,
                )
            )
            for situation_id in (goal_id, terminal_id):
                session.add(
                    SituationGoal(
                        situation_id=situation_id,
                        goal_id=active_goal_id,
                        workspace_id=scope.workspace_id,
                        user_id=scope.user_id,
                        relevance=Decimal("0.8"),
                        contribution=GoalContribution.CONTEXT,
                        reasoning="Relevant",
                        linked_at=NOW,
                    )
                )

    repository = RelevanceRepository(database)
    goals = await repository.list_active_goal_contexts(
        user_id=scope.user_id, workspace_id=scope.workspace_id, limit=20
    )
    situations = await repository.list_situation_contexts(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        correlation_keys=("gmail-thread:t1",),
        goal_ids=tuple(goal.id for goal in goals),
        limit=5,
    )

    assert [goal.id for goal in goals] == [active_goal_id]
    assert [item.id for item in situations] == [exact_id, goal_id]


def _goal(
    user_id: UUID,
    workspace_id: UUID,
    goal_id: UUID,
    status: GoalStatus,
    priority: int,
) -> Goal:
    return Goal(
        id=goal_id,
        user_id=user_id,
        workspace_id=workspace_id,
        title=f"Goal {goal_id}",
        objective="Keep this outcome on track",
        domain="work",
        mode=GoalMode.ACHIEVE,
        priority=priority,
        status=status,
        success_criteria=[],
        constraints={},
        autonomy_policy=dict(SAFE_AUTONOMY_POLICY),
        source=GoalSource.USER_EXPLICIT,
        confidence=Decimal("1"),
        parent_goal_id=None,
    )


def _situation(
    user_id: UUID,
    workspace_id: UUID,
    situation_id: UUID,
    attention: AttentionLevel,
    activity: datetime,
    lifecycle: SituationLifecycle = SituationLifecycle.OPEN,
) -> Situation:
    return Situation(
        id=situation_id,
        user_id=user_id,
        workspace_id=workspace_id,
        type=SituationType.EMAIL_THREAD,
        title=f"Situation {situation_id}",
        lifecycle=lifecycle,
        attention=attention,
        summary="Summary",
        current_state="OPEN",
        next_action=None,
        next_expected=None,
        version=1,
        last_activity_at=activity,
    )
