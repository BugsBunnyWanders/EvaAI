from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from eva_ai.memory.ranking import rank_episodes
from eva_ai.memory.types import (
    EpisodicMemoryRecord,
    EpisodicMemoryStatus,
    MemoryEpisodeType,
    MemorySourceType,
    SemanticCandidate,
)

USER_ID = UUID("00000000-0000-7000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000002")
GOAL_ID = UUID("00000000-0000-7000-8000-000000000003")
NOW = datetime(2026, 9, 6, tzinfo=UTC)


def candidate(
    memory_id: int,
    *,
    distance: float,
    importance: str,
    days_old: int,
    entities: tuple[str, ...] = (),
    goal_ids: tuple[UUID, ...] = (),
) -> SemanticCandidate:
    return SemanticCandidate(
        memory=EpisodicMemoryRecord(
            id=UUID(int=memory_id),
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            type=MemoryEpisodeType.DECISION,
            summary=f"Episode {memory_id}",
            entities=entities,
            goal_ids=goal_ids,
            situation_id=None,
            importance=Decimal(importance),
            confidence=Decimal("1"),
            source_type=MemorySourceType.USER_EXPLICIT,
            source_ref="operator:test",
            idempotency_key=f"episode:{memory_id}",
            occurred_at=NOW - timedelta(days=days_old),
            status=EpisodicMemoryStatus.ACTIVE,
            embedding_model="embedding-test",
            embedding_dimensions=3,
            embedding_input_digest="a" * 64,
            embedded_at=NOW,
            created_at=NOW,
        ),
        distance=distance,
    )


def test_hybrid_ranking_uses_semantics_importance_recency_entities_and_goals() -> None:
    semantic = candidate(1, distance=0.05, importance="0.2", days_old=90)
    contextual = candidate(
        2,
        distance=0.15,
        importance="1",
        days_old=1,
        entities=("acme",),
        goal_ids=(GOAL_ID,),
    )

    ranked = rank_episodes(
        (semantic, contextual),
        now=NOW,
        query_entities=("acme",),
        goal_ids=(GOAL_ID,),
        limit=2,
    )

    assert [item.memory.id for item in ranked] == [UUID(int=2), UUID(int=1)]
    assert ranked[0].score > ranked[1].score


def test_hybrid_ranking_clamps_similarity_and_breaks_ties_by_id() -> None:
    later_id = candidate(2, distance=2.0, importance="0", days_old=0)
    earlier_id = candidate(1, distance=2.0, importance="0", days_old=0)

    ranked = rank_episodes((later_id, earlier_id), now=NOW, query_entities=(), goal_ids=(), limit=1)

    assert ranked[0].memory.id == UUID(int=1)
    assert ranked[0].semantic_similarity == 0
