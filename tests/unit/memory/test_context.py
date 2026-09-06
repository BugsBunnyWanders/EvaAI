from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from eva_ai.memory.context import ContextBounds, ContextRepository, MemoryContextBuilder
from eva_ai.memory.embedding import EmbeddedText, EmbeddingService, ScriptedEmbeddingProvider
from eva_ai.memory.types import (
    ContextGoal,
    ContextSituation,
    EpisodicMemoryRecord,
    EpisodicMemoryStatus,
    MemoryEpisodeType,
    MemoryFactRecord,
    MemoryFactStatus,
    MemoryScopeType,
    MemorySourceType,
    SemanticCandidate,
)

USER_ID = UUID("00000000-0000-7000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000002")
SITUATION_ID = UUID("00000000-0000-7000-8000-000000000003")
GOAL_ID = UUID("00000000-0000-7000-8000-000000000004")
NOW = datetime(2026, 9, 6, tzinfo=UTC)


def situation() -> ContextSituation:
    return ContextSituation(
        id=SITUATION_ID,
        title="Acme platform role",
        summary="Interview process is active",
        current_state="RECRUITER_SCREEN",
        next_action="Prepare questions",
        next_expected="Recruiter confirms schedule",
        attention="HIGH",
        last_activity_at=NOW,
    )


def goal() -> ContextGoal:
    return ContextGoal(
        id=GOAL_ID,
        title="Find infrastructure role",
        objective="Choose a strong backend or AI infrastructure opportunity",
        domain="career",
        priority=90,
    )


def fact() -> MemoryFactRecord:
    return MemoryFactRecord(
        id=UUID(int=10),
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        namespace="career.preferences",
        key="interview_time",
        value_json={"period": "afternoon"},
        scope_type=MemoryScopeType.WORKSPACE,
        scope_id=WORKSPACE_ID,
        source_type=MemorySourceType.USER_EXPLICIT,
        source_ref="operator:test",
        confidence=Decimal("1"),
        idempotency_key="fact:test",
        status=MemoryFactStatus.ACTIVE,
        valid_from=NOW,
        valid_until=None,
        supersedes_memory_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def episode() -> EpisodicMemoryRecord:
    return EpisodicMemoryRecord(
        id=UUID(int=20),
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        type=MemoryEpisodeType.DECISION,
        summary="User previously chose an infrastructure-heavy role at Acme.",
        entities=("acme",),
        goal_ids=(GOAL_ID,),
        situation_id=None,
        importance=Decimal("0.9"),
        confidence=Decimal("1"),
        source_type=MemorySourceType.USER_EXPLICIT,
        source_ref="operator:test",
        idempotency_key="episode:test",
        occurred_at=NOW,
        status=EpisodicMemoryStatus.ACTIVE,
        embedding_model="embedding-test",
        embedding_dimensions=3,
        embedding_input_digest="a" * 64,
        embedded_at=NOW,
        created_at=NOW,
    )


class FakeContextRepository:
    def __init__(self, *, has_episodes: bool) -> None:
        self.has_episodes = has_episodes
        self.semantic_calls = 0

    async def load_context_subject(
        self, *, user_id: UUID, workspace_id: UUID, situation_id: UUID
    ) -> tuple[ContextSituation, tuple[ContextGoal, ...]]:
        return situation(), (goal(),)

    async def list_context_facts(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID,
        goal_ids: tuple[UUID, ...],
        at: datetime,
        limit: int,
    ) -> tuple[MemoryFactRecord, ...]:
        return (fact(),)

    async def has_active_episodes(self, *, user_id: UUID, workspace_id: UUID) -> bool:
        return self.has_episodes

    async def semantic_candidates(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        embedding: EmbeddedText,
        limit: int,
    ) -> tuple[SemanticCandidate, ...]:
        self.semantic_calls += 1
        return (SemanticCandidate(memory=episode(), distance=0.1),)


def builder(
    repository: ContextRepository, provider: ScriptedEmbeddingProvider
) -> MemoryContextBuilder:
    embedding = EmbeddingService(provider, "embedding-test", 3, 4000, lambda: NOW)
    return MemoryContextBuilder(
        repository,
        embedding,
        ContextBounds(),
        clock=lambda: NOW,
    )


async def test_context_builder_skips_embedding_without_episodes() -> None:
    repository = FakeContextRepository(has_episodes=False)
    provider = ScriptedEmbeddingProvider(())

    context = await builder(repository, provider).build_for_situation(
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        situation_id=SITUATION_ID,
        focus=None,
    )

    assert context.situation.id == SITUATION_ID
    assert context.goals[0].id == GOAL_ID
    assert context.facts[0].key == "interview_time"
    assert context.episodes == ()
    assert len(context.digest) == 64
    assert provider.inputs == []
    assert repository.semantic_calls == 0


async def test_context_builder_embeds_bounded_subject_and_reranks_episodes() -> None:
    repository = FakeContextRepository(has_episodes=True)
    provider = ScriptedEmbeddingProvider(((1.0, 0.0, 0.0),))

    context = await builder(repository, provider).build_for_situation(
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        situation_id=SITUATION_ID,
        focus="Acme interview preparation",
    )

    assert context.episodes[0].memory.id == UUID(int=20)
    assert context.episodes[0].semantic_similarity == 0.9
    assert "Acme interview preparation" in provider.inputs[0]
    assert repository.semantic_calls == 1
