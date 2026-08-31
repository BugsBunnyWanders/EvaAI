from decimal import Decimal
from uuid import UUID

from eva_ai.goals.errors import GoalNotFoundError
from eva_ai.goals.repository import GoalRepository
from eva_ai.goals.types import (
    GoalDraft,
    GoalRecord,
    GoalSource,
    GoalStatus,
    GoalUpdate,
    InferredGoalDraft,
)


class GoalService:
    def __init__(self, repository: GoalRepository) -> None:
        self._repository = repository

    async def create_explicit(self, draft: GoalDraft) -> GoalRecord:
        return await self._repository.create(
            draft,
            source=GoalSource.USER_EXPLICIT,
            status=GoalStatus.ACTIVE,
            confidence=Decimal("1"),
        )

    async def create_inferred(self, draft: InferredGoalDraft) -> GoalRecord:
        return await self._repository.create(
            draft,
            source=GoalSource.AGENT_INFERRED,
            status=GoalStatus.CANDIDATE,
            confidence=draft.confidence,
        )

    async def get(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        goal_id: UUID,
    ) -> GoalRecord:
        goal = await self._repository.get(
            user_id=user_id,
            workspace_id=workspace_id,
            goal_id=goal_id,
        )
        if goal is None:
            raise GoalNotFoundError
        return goal

    async def list(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        statuses: tuple[GoalStatus, ...] = (),
        limit: int = 50,
    ) -> tuple[GoalRecord, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Goal list limit must be between 1 and 100")
        return await self._repository.list(
            user_id=user_id,
            workspace_id=workspace_id,
            statuses=statuses,
            limit=limit,
        )

    async def update(self, command: GoalUpdate) -> GoalRecord:
        return await self._repository.update(command)
