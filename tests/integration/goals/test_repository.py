from decimal import Decimal
from uuid import uuid7

import pytest
from sqlalchemy import func, select

from eva_ai.db import Database
from eva_ai.db.models import Goal
from eva_ai.goals import (
    GoalDraft,
    GoalMode,
    GoalParentError,
    GoalRepository,
    GoalSource,
    GoalStatus,
    GoalUpdate,
)
from tests.integration.factories import Scope, create_scope


def draft(scope: Scope, title: str, priority: int = 50, parent_id: object = None) -> GoalDraft:
    return GoalDraft(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        title=title,
        objective=f"Complete {title}",
        domain="personal",
        mode=GoalMode.ACHIEVE,
        priority=priority,
        success_criteria=(f"{title} completed",),
        constraints={"budget": "bounded"},
        parent_goal_id=parent_id,
    )


@pytest.mark.integration
async def test_repository_creates_and_gets_goal_only_in_exact_scope(database: Database) -> None:
    scope = await create_scope(database)
    other = await create_scope(database)
    repository = GoalRepository(database)

    created = await repository.create(
        draft(scope, "Book trip"),
        source=GoalSource.USER_EXPLICIT,
        status=GoalStatus.ACTIVE,
        confidence=Decimal("1"),
    )

    assert created.title == "Book trip"
    assert created.autonomy_policy == {"mode": "REQUIRE_APPROVAL"}
    assert (
        await repository.get(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            goal_id=created.id,
        )
        == created
    )
    assert (
        await repository.get(
            user_id=other.user_id,
            workspace_id=scope.workspace_id,
            goal_id=created.id,
        )
        is None
    )
    assert (
        await repository.get(
            user_id=scope.user_id,
            workspace_id=other.workspace_id,
            goal_id=created.id,
        )
        is None
    )


@pytest.mark.integration
async def test_repository_accepts_same_scope_parent_and_rejects_cross_scope_atomically(
    database: Database,
) -> None:
    scope = await create_scope(database)
    other = await create_scope(database)
    repository = GoalRepository(database)
    parent = await repository.create(
        draft(scope, "Parent"),
        source=GoalSource.USER_EXPLICIT,
        status=GoalStatus.ACTIVE,
        confidence=Decimal("1"),
    )
    child = await repository.create(
        draft(scope, "Child", parent_id=parent.id),
        source=GoalSource.USER_EXPLICIT,
        status=GoalStatus.ACTIVE,
        confidence=Decimal("1"),
    )
    before_count = await _goal_count(database)

    assert child.parent_goal_id == parent.id
    with pytest.raises(GoalParentError):
        await repository.create(
            draft(other, "Invalid child", parent_id=parent.id),
            source=GoalSource.USER_EXPLICIT,
            status=GoalStatus.ACTIVE,
            confidence=Decimal("1"),
        )
    assert await _goal_count(database) == before_count


@pytest.mark.integration
async def test_repository_lists_by_status_in_deterministic_priority_order(
    database: Database,
) -> None:
    scope = await create_scope(database)
    repository = GoalRepository(database)
    low_first = await repository.create(
        draft(scope, "Low first", priority=10),
        source=GoalSource.USER_EXPLICIT,
        status=GoalStatus.ACTIVE,
        confidence=Decimal("1"),
    )
    low_second = await repository.create(
        draft(scope, "Low second", priority=10),
        source=GoalSource.USER_EXPLICIT,
        status=GoalStatus.ACTIVE,
        confidence=Decimal("1"),
    )
    high = await repository.create(
        draft(scope, "High", priority=90),
        source=GoalSource.AGENT_INFERRED,
        status=GoalStatus.CANDIDATE,
        confidence=Decimal("0.8"),
    )

    assert await repository.list(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        limit=3,
    ) == (high, low_first, low_second)
    assert await repository.list(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        statuses=(GoalStatus.ACTIVE,),
        limit=10,
    ) == (low_first, low_second)


@pytest.mark.integration
async def test_repository_invalid_parent_update_rolls_back_other_changes(
    database: Database,
) -> None:
    scope = await create_scope(database)
    repository = GoalRepository(database)
    goal = await repository.create(
        draft(scope, "Original"),
        source=GoalSource.USER_EXPLICIT,
        status=GoalStatus.ACTIVE,
        confidence=Decimal("1"),
    )

    with pytest.raises(GoalParentError):
        await repository.update(
            GoalUpdate(
                user_id=scope.user_id,
                workspace_id=scope.workspace_id,
                goal_id=goal.id,
                title="Must roll back",
                parent_goal_id=uuid7(),
            )
        )

    unchanged = await repository.get(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        goal_id=goal.id,
    )
    assert unchanged is not None
    assert unchanged.title == "Original"
    assert unchanged.parent_goal_id is None


async def _goal_count(database: Database) -> int:
    async with database.session() as session:
        return int(await session.scalar(select(func.count()).select_from(Goal)) or 0)
