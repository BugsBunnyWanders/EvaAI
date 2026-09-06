from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from eva_ai.memory.embedding import EmbeddingService
from eva_ai.memory.errors import MemoryNotFoundError
from eva_ai.memory.policy import MemoryPolicy
from eva_ai.memory.ranking import query_entities, rank_episodes
from eva_ai.memory.repository import MemoryRepository
from eva_ai.memory.types import (
    EpisodicMemoryDraft,
    EpisodicMemoryRecord,
    EpisodicMemoryStatus,
    MemoryFactDraft,
    MemoryFactRecord,
    MemoryFactStatus,
    RankedEpisode,
)


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        policy: MemoryPolicy,
        embedding: EmbeddingService | None = None,
        episode_candidate_limit: int = 50,
        episode_limit: int = 8,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._embedding = embedding
        self._episode_candidate_limit = episode_candidate_limit
        self._episode_limit = episode_limit
        self._clock = clock or (lambda: datetime.now(UTC))

    async def put_fact(self, draft: MemoryFactDraft) -> MemoryFactRecord:
        self._policy.validate_fact(draft)
        return await self._repository.put_fact(draft)

    async def get_fact(
        self, *, user_id: UUID, workspace_id: UUID, memory_id: UUID
    ) -> MemoryFactRecord:
        record = await self._repository.get_fact(
            user_id=user_id, workspace_id=workspace_id, memory_id=memory_id
        )
        if record is None:
            raise MemoryNotFoundError
        return record

    async def list_facts(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        statuses: tuple[MemoryFactStatus, ...] = (),
        limit: int = 50,
    ) -> tuple[MemoryFactRecord, ...]:
        return await self._repository.list_facts(
            user_id=user_id,
            workspace_id=workspace_id,
            statuses=statuses,
            limit=limit,
        )

    async def retract_fact(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        memory_id: UUID,
        retracted_at: datetime | None = None,
    ) -> MemoryFactRecord:
        return await self._repository.retract_fact(
            user_id=user_id,
            workspace_id=workspace_id,
            memory_id=memory_id,
            retracted_at=retracted_at or self._clock(),
        )

    async def create_episode(self, draft: EpisodicMemoryDraft) -> EpisodicMemoryRecord:
        existing = await self._repository.find_episode_replay(draft)
        if existing is not None:
            return existing
        if self._embedding is None:
            raise ValueError("Episodic memory embedding is not configured")
        # The provider call completes before the repository opens its write transaction.
        embedded = await self._embedding.embed(draft.summary)
        return await self._repository.create_episode(draft, embedded)

    async def get_episode(
        self, *, user_id: UUID, workspace_id: UUID, memory_id: UUID
    ) -> EpisodicMemoryRecord:
        record = await self._repository.get_episode(
            user_id=user_id, workspace_id=workspace_id, memory_id=memory_id
        )
        if record is None:
            raise MemoryNotFoundError
        return record

    async def list_episodes(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        statuses: tuple[EpisodicMemoryStatus, ...] = (),
        limit: int = 50,
    ) -> tuple[EpisodicMemoryRecord, ...]:
        return await self._repository.list_episodes(
            user_id=user_id,
            workspace_id=workspace_id,
            statuses=statuses,
            limit=limit,
        )

    async def retract_episode(
        self, *, user_id: UUID, workspace_id: UUID, memory_id: UUID
    ) -> EpisodicMemoryRecord:
        return await self._repository.retract_episode(
            user_id=user_id, workspace_id=workspace_id, memory_id=memory_id
        )

    async def search_episodes(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        query: str,
        limit: int | None = None,
    ) -> tuple[RankedEpisode, ...]:
        if self._embedding is None:
            raise ValueError("Episodic memory embedding is not configured")
        result_limit = limit or self._episode_limit
        if not 1 <= result_limit <= self._episode_limit:
            raise ValueError("Episode search limit exceeds configured maximum")
        embedded = await self._embedding.embed(query)
        candidates = await self._repository.semantic_candidates(
            user_id=user_id,
            workspace_id=workspace_id,
            embedding=embedded,
            limit=self._episode_candidate_limit,
        )
        return rank_episodes(
            candidates,
            now=self._clock(),
            query_entities=query_entities(query),
            goal_ids=(),
            limit=result_limit,
        )
