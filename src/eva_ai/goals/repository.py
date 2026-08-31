from decimal import Decimal
from typing import cast
from uuid import UUID, uuid7

from pydantic import JsonValue
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from eva_ai.db.models import Goal, Workspace
from eva_ai.db.session import Database
from eva_ai.goals.errors import GoalNotFoundError, GoalParentError, GoalScopeError
from eva_ai.goals.transitions import validate_goal_transition
from eva_ai.goals.types import (
    SAFE_AUTONOMY_POLICY,
    GoalDraft,
    GoalRecord,
    GoalSource,
    GoalStatus,
    GoalUpdate,
)


class GoalRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(
        self,
        draft: GoalDraft,
        *,
        source: GoalSource,
        status: GoalStatus,
        confidence: Decimal,
    ) -> GoalRecord:
        async with self._database.session() as session:
            async with session.begin():
                if not await _scope_exists(session, draft.user_id, draft.workspace_id):
                    raise GoalScopeError
                if draft.parent_goal_id is not None and not await _goal_exists(
                    session,
                    draft.user_id,
                    draft.workspace_id,
                    draft.parent_goal_id,
                ):
                    raise GoalParentError
                goal = Goal(
                    id=uuid7(),
                    user_id=draft.user_id,
                    workspace_id=draft.workspace_id,
                    title=draft.title,
                    objective=draft.objective,
                    domain=draft.domain,
                    mode=draft.mode,
                    priority=draft.priority,
                    status=status,
                    success_criteria=list(draft.success_criteria),
                    constraints=dict(draft.constraints),
                    autonomy_policy=dict(SAFE_AUTONOMY_POLICY),
                    source=source,
                    confidence=confidence,
                    parent_goal_id=draft.parent_goal_id,
                )
                session.add(goal)
                await session.flush()
                return _goal_record(goal)

    async def get(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        goal_id: UUID,
    ) -> GoalRecord | None:
        statement = select(Goal).where(
            Goal.id == goal_id,
            Goal.user_id == user_id,
            Goal.workspace_id == workspace_id,
        )
        async with self._database.session() as session:
            goal = await session.scalar(statement)
        return _goal_record(goal) if goal is not None else None

    async def list(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        statuses: tuple[GoalStatus, ...] = (),
        limit: int = 50,
    ) -> tuple[GoalRecord, ...]:
        statement = (
            select(Goal)
            .where(Goal.user_id == user_id, Goal.workspace_id == workspace_id)
            .order_by(desc(Goal.priority), Goal.created_at, Goal.id)
            .limit(limit)
        )
        if statuses:
            statement = statement.where(Goal.status.in_(statuses))
        async with self._database.session() as session:
            goals = (await session.scalars(statement)).all()
        return tuple(_goal_record(goal) for goal in goals)

    async def update(self, command: GoalUpdate) -> GoalRecord:
        statement = (
            select(Goal)
            .where(
                Goal.id == command.goal_id,
                Goal.user_id == command.user_id,
                Goal.workspace_id == command.workspace_id,
            )
            .with_for_update()
        )
        async with self._database.session() as session:
            async with session.begin():
                goal = await session.scalar(statement)
                if goal is None:
                    raise GoalNotFoundError
                if command.status is not None:
                    validate_goal_transition(GoalStatus(goal.status), command.status)
                if command.parent_goal_id is not None:
                    if command.parent_goal_id == command.goal_id or not await _goal_exists(
                        session,
                        command.user_id,
                        command.workspace_id,
                        command.parent_goal_id,
                    ):
                        raise GoalParentError

                for field_name in (
                    "title",
                    "objective",
                    "domain",
                    "mode",
                    "priority",
                    "success_criteria",
                    "constraints",
                    "status",
                ):
                    value = getattr(command, field_name)
                    if value is None:
                        continue
                    if field_name == "success_criteria":
                        value = list(value)
                    elif field_name == "constraints":
                        value = dict(value)
                    setattr(goal, field_name, value)
                if command.clear_parent:
                    goal.parent_goal_id = None
                elif command.parent_goal_id is not None:
                    goal.parent_goal_id = command.parent_goal_id
                await session.flush()
                return _goal_record(goal)


async def _scope_exists(session: AsyncSession, user_id: UUID, workspace_id: UUID) -> bool:
    statement = select(Workspace.id).where(
        Workspace.id == workspace_id,
        Workspace.user_id == user_id,
    )
    return await session.scalar(statement) is not None


async def _goal_exists(
    session: AsyncSession,
    user_id: UUID,
    workspace_id: UUID,
    goal_id: UUID,
) -> bool:
    statement = select(Goal.id).where(
        Goal.id == goal_id,
        Goal.user_id == user_id,
        Goal.workspace_id == workspace_id,
    )
    return await session.scalar(statement) is not None


def _goal_record(goal: Goal) -> GoalRecord:
    return GoalRecord(
        id=goal.id,
        user_id=goal.user_id,
        workspace_id=goal.workspace_id,
        title=goal.title,
        objective=goal.objective,
        domain=goal.domain,
        mode=goal.mode,
        priority=goal.priority,
        status=goal.status,
        success_criteria=tuple(cast(list[str], goal.success_criteria)),
        constraints=cast(dict[str, JsonValue], dict(goal.constraints)),
        autonomy_policy=cast(dict[str, JsonValue], dict(goal.autonomy_policy)),
        source=goal.source,
        confidence=goal.confidence,
        parent_goal_id=goal.parent_goal_id,
        created_at=goal.created_at,
        updated_at=goal.updated_at,
    )
