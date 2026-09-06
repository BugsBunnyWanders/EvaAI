import re
from datetime import datetime
from uuid import UUID

from eva_ai.memory.types import RankedEpisode, SemanticCandidate

_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]+")


def rank_episodes(
    candidates: tuple[SemanticCandidate, ...],
    *,
    now: datetime,
    query_entities: tuple[str, ...],
    goal_ids: tuple[UUID, ...],
    limit: int,
) -> tuple[RankedEpisode, ...]:
    if not 1 <= limit <= 20:
        raise ValueError("Ranked episode limit must be between 1 and 20")
    entity_targets = set(query_entities)
    goal_targets = set(goal_ids)
    ranked = []
    for candidate in candidates:
        memory = candidate.memory
        similarity = min(1.0, max(0.0, 1.0 - candidate.distance))
        age_days = max(0.0, (now - memory.occurred_at).total_seconds() / 86_400)
        recency = 1.0 / (1.0 + age_days / 30.0)
        entity_overlap = _overlap(set(memory.entities), entity_targets)
        goal_overlap = _overlap(set(memory.goal_ids), goal_targets)
        # Semantic relevance leads, while durable user context can promote a slightly less similar
        # episode that is important, recent, or explicitly linked to the current work.
        score = (
            0.55 * similarity
            + 0.15 * float(memory.importance)
            + 0.10 * recency
            + 0.10 * entity_overlap
            + 0.10 * goal_overlap
        )
        ranked.append(
            RankedEpisode(
                memory=memory,
                score=min(1.0, max(0.0, score)),
                semantic_similarity=similarity,
            )
        )
    ranked.sort(key=lambda item: (-item.score, item.memory.id))
    return tuple(ranked[:limit])


def query_entities(query: str) -> tuple[str, ...]:
    return tuple(sorted(set(_WORD.findall(query.casefold()))))


def _overlap[T](values: set[T], targets: set[T]) -> float:
    if not targets:
        return 0.0
    return len(values & targets) / len(targets)
