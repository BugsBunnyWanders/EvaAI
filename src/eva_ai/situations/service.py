from uuid import UUID

from eva_ai.situations.errors import SituationNotFoundError
from eva_ai.situations.repository import SituationRepository
from eva_ai.situations.types import (
    AttentionLevel,
    LinkSituationGoal,
    SituationGoalRecord,
    SituationLifecycle,
    SituationRecord,
    SituationSnapshotUpdate,
)


class SituationService:
    def __init__(self, repository: SituationRepository) -> None:
        self._repository = repository

    async def get(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID,
    ) -> SituationRecord:
        situation = await self._repository.get(
            user_id=user_id,
            workspace_id=workspace_id,
            situation_id=situation_id,
        )
        if situation is None:
            raise SituationNotFoundError
        return situation

    async def list(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        lifecycles: tuple[SituationLifecycle, ...] = (),
        limit: int = 50,
    ) -> tuple[SituationRecord, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Situation list limit must be between 1 and 100")
        return await self._repository.list(
            user_id=user_id,
            workspace_id=workspace_id,
            lifecycles=lifecycles,
            limit=limit,
        )

    async def update_snapshot(self, command: SituationSnapshotUpdate) -> SituationRecord:
        return await self._repository.update_snapshot(command)

    async def update_lifecycle(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID,
        lifecycle: SituationLifecycle,
    ) -> SituationRecord:
        return await self._repository.update_lifecycle(
            user_id=user_id,
            workspace_id=workspace_id,
            situation_id=situation_id,
            lifecycle=lifecycle,
        )

    async def update_attention(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID,
        attention: AttentionLevel,
    ) -> SituationRecord:
        return await self._repository.update_attention(
            user_id=user_id,
            workspace_id=workspace_id,
            situation_id=situation_id,
            attention=attention,
        )

    async def link_goal(self, command: LinkSituationGoal) -> SituationGoalRecord:
        return await self._repository.link_goal(command)
