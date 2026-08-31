from decimal import Decimal
from uuid import uuid7

import pytest

from eva_ai.db import Database
from eva_ai.goals import (
    GoalDraft,
    GoalMode,
    GoalNotFoundError,
    GoalRepository,
    GoalService,
    GoalSource,
    GoalStatus,
    GoalUpdate,
    InferredGoalDraft,
    InvalidGoalTransitionError,
)
from tests.integration.factories import Scope, create_scope


def explicit_draft(scope: Scope, title: str = "Prepare launch") -> GoalDraft:
    return GoalDraft(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        title=title,
        objective="Prepare a reliable launch",
        domain="work",
        mode=GoalMode.ACHIEVE,
    )


@pytest.mark.integration
async def test_service_applies_explicit_and_inferred_creation_policy(database: Database) -> None:
    scope = await create_scope(database)
    service = GoalService(GoalRepository(database))

    explicit = await service.create_explicit(explicit_draft(scope))
    inferred = await service.create_inferred(
        InferredGoalDraft(
            **explicit_draft(scope, "Possible follow-up").model_dump(),
            confidence=Decimal("0.625"),
        )
    )

    assert (explicit.source, explicit.status, explicit.confidence) == (
        GoalSource.USER_EXPLICIT,
        GoalStatus.ACTIVE,
        Decimal("1.000"),
    )
    assert (inferred.source, inferred.status, inferred.confidence) == (
        GoalSource.AGENT_INFERRED,
        GoalStatus.CANDIDATE,
        Decimal("0.625"),
    )
    assert explicit.autonomy_policy == {"mode": "REQUIRE_APPROVAL"}
    assert inferred.autonomy_policy == {"mode": "REQUIRE_APPROVAL"}


@pytest.mark.integration
async def test_service_updates_fields_parent_and_lifecycle_atomically(database: Database) -> None:
    scope = await create_scope(database)
    service = GoalService(GoalRepository(database))
    parent = await service.create_explicit(explicit_draft(scope, "Parent"))
    goal = await service.create_explicit(explicit_draft(scope, "Child"))

    paused = await service.update(
        GoalUpdate(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            goal_id=goal.id,
            title="Updated child",
            priority=80,
            parent_goal_id=parent.id,
            status=GoalStatus.PAUSED,
        )
    )
    resumed = await service.update(
        GoalUpdate(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            goal_id=goal.id,
            clear_parent=True,
            status=GoalStatus.ACTIVE,
        )
    )

    assert (paused.title, paused.priority, paused.parent_goal_id, paused.status) == (
        "Updated child",
        80,
        parent.id,
        GoalStatus.PAUSED,
    )
    assert resumed.parent_goal_id is None
    assert resumed.status is GoalStatus.ACTIVE


@pytest.mark.integration
async def test_service_rejects_terminal_reopen_without_partial_update(database: Database) -> None:
    scope = await create_scope(database)
    service = GoalService(GoalRepository(database))
    goal = await service.create_explicit(explicit_draft(scope))
    completed = await service.update(
        GoalUpdate(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            goal_id=goal.id,
            status=GoalStatus.COMPLETED,
        )
    )

    with pytest.raises(InvalidGoalTransitionError):
        await service.update(
            GoalUpdate(
                user_id=scope.user_id,
                workspace_id=scope.workspace_id,
                goal_id=goal.id,
                title="Must not persist",
                status=GoalStatus.ACTIVE,
            )
        )

    unchanged = await service.get(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        goal_id=goal.id,
    )
    assert unchanged.title == completed.title
    assert unchanged.status is GoalStatus.COMPLETED


@pytest.mark.integration
async def test_service_hides_missing_and_wrong_scope_as_not_found(database: Database) -> None:
    scope = await create_scope(database)
    other = await create_scope(database)
    service = GoalService(GoalRepository(database))
    goal = await service.create_explicit(explicit_draft(scope))

    with pytest.raises(GoalNotFoundError):
        await service.get(
            user_id=other.user_id,
            workspace_id=other.workspace_id,
            goal_id=goal.id,
        )
    with pytest.raises(GoalNotFoundError):
        await service.get(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            goal_id=uuid7(),
        )


@pytest.mark.integration
@pytest.mark.parametrize("limit", [0, 101])
async def test_service_rejects_unbounded_list_limits(database: Database, limit: int) -> None:
    scope = await create_scope(database)
    service = GoalService(GoalRepository(database))

    with pytest.raises(ValueError, match="Goal list limit must be between 1 and 100"):
        await service.list(
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            limit=limit,
        )
