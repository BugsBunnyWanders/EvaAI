import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from eva_ai.memory.embedding import EmbeddedText, EmbeddingService
from eva_ai.memory.errors import MemoryEmbeddingError, MemoryNotFoundError
from eva_ai.memory.ranking import query_entities, rank_episodes
from eva_ai.memory.types import (
    AgentWorkingContext,
    ContextGoal,
    ContextSituation,
    MemoryFactRecord,
    RankedEpisode,
    SemanticCandidate,
)


@dataclass(frozen=True, slots=True)
class ContextBounds:
    fact_limit: int = 20
    fact_total_chars: int = 8000
    episode_candidate_limit: int = 50
    episode_limit: int = 8
    episode_total_chars: int = 6000
    query_max_chars: int = 4000


class ContextRepository(Protocol):
    async def load_context_subject(
        self, *, user_id: UUID, workspace_id: UUID, situation_id: UUID
    ) -> tuple[ContextSituation, tuple[ContextGoal, ...]] | None: ...

    async def list_context_facts(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID,
        goal_ids: tuple[UUID, ...],
        at: datetime,
        limit: int,
    ) -> tuple[MemoryFactRecord, ...]: ...

    async def has_active_episodes(self, *, user_id: UUID, workspace_id: UUID) -> bool: ...

    async def semantic_candidates(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        embedding: EmbeddedText,
        limit: int,
    ) -> tuple[SemanticCandidate, ...]: ...


class MemoryContextBuilder:
    def __init__(
        self,
        repository: ContextRepository,
        embedding: EmbeddingService,
        bounds: ContextBounds,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._embedding = embedding
        self._bounds = bounds
        self._clock = clock

    async def build_for_situation(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID,
        focus: str | None,
    ) -> AgentWorkingContext:
        built_at = self._clock()
        subject = await self._repository.load_context_subject(
            user_id=user_id,
            workspace_id=workspace_id,
            situation_id=situation_id,
        )
        if subject is None:
            raise MemoryNotFoundError
        situation, goals = subject
        goal_ids = tuple(goal.id for goal in goals)
        facts = await self._repository.list_context_facts(
            user_id=user_id,
            workspace_id=workspace_id,
            situation_id=situation_id,
            goal_ids=goal_ids,
            at=built_at,
            limit=self._bounds.fact_limit,
        )
        bounded_facts = _bound_facts(facts, self._bounds.fact_total_chars)
        episodes: tuple[RankedEpisode, ...] = ()
        if await self._repository.has_active_episodes(user_id=user_id, workspace_id=workspace_id):
            query = _query_text(situation, goals, focus, self._bounds.query_max_chars)
            try:
                query_embedding = await self._embedding.embed(query)
            except MemoryEmbeddingError:
                # Structured context remains useful during a provider outage; callers can decide
                # whether episodic degradation is acceptable for their operation.
                query_embedding = None
            if query_embedding is not None:
                candidates = await self._repository.semantic_candidates(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    embedding=query_embedding,
                    limit=self._bounds.episode_candidate_limit,
                )
                ranked = rank_episodes(
                    candidates,
                    now=built_at,
                    query_entities=query_entities(query),
                    goal_ids=goal_ids,
                    limit=self._bounds.episode_limit,
                )
                episodes = _bound_episodes(ranked, self._bounds.episode_total_chars)

        payload = {
            "schema_version": 1,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "situation": situation,
            "goals": goals,
            "facts": bounded_facts,
            "episodes": episodes,
            "built_at": built_at,
        }
        digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
        return AgentWorkingContext(**payload, digest=digest)


def serialize_context(context: AgentWorkingContext) -> str:
    return _canonical_json(context.model_dump(mode="json"))


def _query_text(
    situation: ContextSituation,
    goals: tuple[ContextGoal, ...],
    focus: str | None,
    limit: int,
) -> str:
    parts = [
        situation.title,
        situation.summary,
        situation.current_state,
        situation.next_action or "",
        situation.next_expected or "",
        focus.strip() if focus else "",
    ]
    for goal in goals:
        parts.extend((goal.title, goal.objective, goal.domain))
    return "\n".join(part for part in parts if part)[:limit]


def _bound_facts(
    facts: tuple[MemoryFactRecord, ...], total_chars: int
) -> tuple[MemoryFactRecord, ...]:
    selected = []
    remaining = total_chars
    for fact in facts:
        size = len(fact.model_dump_json())
        if size > remaining:
            continue
        selected.append(fact)
        remaining -= size
    return tuple(selected)


def _bound_episodes(
    episodes: tuple[RankedEpisode, ...], total_chars: int
) -> tuple[RankedEpisode, ...]:
    selected = []
    remaining = total_chars
    for episode in episodes:
        size = len(episode.memory.summary)
        if size > remaining:
            continue
        selected.append(episode)
        remaining -= size
    return tuple(selected)


def _canonical_json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
