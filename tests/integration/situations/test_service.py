from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid7

import pytest

from eva_ai.db import Database
from eva_ai.goals import GoalDraft, GoalMode, GoalRepository, GoalService
from eva_ai.situations import (
    AttentionLevel,
    GoalContribution,
    InvalidSituationTransitionError,
    LinkSituationGoal,
    SituationLifecycle,
    SituationNotFoundError,
    SituationRepository,
    SituationService,
)
from tests.integration.factories import create_scope
from tests.integration.situations.test_repository import create_situation

NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


@pytest.mark.integration
async def test_service_updates_lifecycle_attention_and_explicit_goal_link(
    database: Database,
) -> None:
    scope = await create_scope(database)
    situation_id = await create_situation(database, scope, title="Service")
    situation_service = SituationService(SituationRepository(database))
    goal = await GoalService(GoalRepository(database)).create_explicit(
        GoalDraft(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            title="Respond",
            objective="Send the required response",
            domain="personal",
            mode=GoalMode.ACHIEVE,
        )
    )

    active = await situation_service.update_lifecycle(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=situation_id,
        lifecycle=SituationLifecycle.ACTIVE,
    )
    urgent = await situation_service.update_attention(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=situation_id,
        attention=AttentionLevel.URGENT,
    )
    relationship = await situation_service.link_goal(
        LinkSituationGoal(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            situation_id=situation_id,
            goal_id=goal.id,
            relevance=Decimal("1"),
            contribution=GoalContribution.SUPPORTS,
            reasoning="Required response",
            linked_at=NOW,
        )
    )

    assert active.lifecycle is SituationLifecycle.ACTIVE
    assert urgent.attention is AttentionLevel.URGENT
    assert relationship.goal_id == goal.id


@pytest.mark.integration
async def test_service_rejects_terminal_reopen_without_changing_attention(
    database: Database,
) -> None:
    scope = await create_scope(database)
    situation_id = await create_situation(database, scope, title="Terminal")
    service = SituationService(SituationRepository(database))
    await service.update_lifecycle(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=situation_id,
        lifecycle=SituationLifecycle.RESOLVED,
    )

    with pytest.raises(InvalidSituationTransitionError):
        await service.update_lifecycle(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            situation_id=situation_id,
            lifecycle=SituationLifecycle.ACTIVE,
        )

    record = await service.get(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        situation_id=situation_id,
    )
    assert record.lifecycle is SituationLifecycle.RESOLVED
    assert record.attention is AttentionLevel.NORMAL


@pytest.mark.integration
async def test_service_hides_missing_scope_and_rejects_unbounded_limit(database: Database) -> None:
    scope = await create_scope(database)
    service = SituationService(SituationRepository(database))

    with pytest.raises(SituationNotFoundError):
        await service.get(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            situation_id=uuid7(),
        )
    with pytest.raises(ValueError, match="Situation list limit must be between 1 and 100"):
        await service.list(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            limit=101,
        )
