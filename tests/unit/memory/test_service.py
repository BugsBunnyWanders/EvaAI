from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest

from eva_ai.memory.embedding import EmbeddedText, EmbeddingService, ScriptedEmbeddingProvider
from eva_ai.memory.errors import UnsafeMemoryError
from eva_ai.memory.policy import MemoryPolicy
from eva_ai.memory.repository import MemoryRepository
from eva_ai.memory.service import MemoryService
from eva_ai.memory.types import (
    EpisodicMemoryDraft,
    EpisodicMemoryRecord,
    EpisodicMemoryStatus,
    MemoryEpisodeType,
    MemoryFactDraft,
    MemoryFactRecord,
    MemoryScopeType,
    MemorySourceType,
)

USER_ID = UUID("00000000-0000-7000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000002")
NOW = datetime(2026, 9, 6, tzinfo=UTC)


class FakeRepository:
    def __init__(self, replay: EpisodicMemoryRecord | None = None) -> None:
        self.replay = replay
        self.fact_called = False

    async def put_fact(self, draft: MemoryFactDraft) -> MemoryFactRecord:
        self.fact_called = True
        raise AssertionError("unsafe facts must not reach repository")

    async def find_episode_replay(self, draft: EpisodicMemoryDraft) -> EpisodicMemoryRecord | None:
        return self.replay

    async def create_episode(
        self, draft: EpisodicMemoryDraft, embedded: EmbeddedText
    ) -> EpisodicMemoryRecord:
        raise AssertionError("replay must not create")


def episode() -> EpisodicMemoryDraft:
    return EpisodicMemoryDraft(
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        type=MemoryEpisodeType.DECISION,
        summary="User preferred the platform role.",
        importance=Decimal("0.8"),
        confidence=Decimal("1"),
        source_type=MemorySourceType.USER_EXPLICIT,
        source_ref="operator:test",
        idempotency_key="episode:test",
        occurred_at=NOW,
    )


async def test_service_rejects_unsafe_fact_before_repository() -> None:
    repository = FakeRepository()
    service = MemoryService(cast(MemoryRepository, repository), MemoryPolicy())
    draft = MemoryFactDraft(
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        namespace="credentials",
        key="password",
        value_json="redacted",
        scope_type=MemoryScopeType.WORKSPACE,
        scope_id=WORKSPACE_ID,
        source_type=MemorySourceType.USER_EXPLICIT,
        source_ref="operator:test",
        confidence=Decimal("1"),
        idempotency_key="unsafe:test",
        valid_from=NOW,
    )

    with pytest.raises(UnsafeMemoryError):
        await service.put_fact(draft)
    assert repository.fact_called is False


async def test_episode_replay_skips_embedding_provider() -> None:
    draft = episode()
    replay = EpisodicMemoryRecord(
        **draft.model_dump(),
        status=EpisodicMemoryStatus.ACTIVE,
        embedding_model="embedding-test",
        embedding_dimensions=3,
        embedding_input_digest="a" * 64,
        embedded_at=NOW,
        created_at=NOW,
    )
    provider = ScriptedEmbeddingProvider(())
    embedding = EmbeddingService(provider, "embedding-test", 3, 4000, lambda: NOW)
    service = MemoryService(
        cast(MemoryRepository, FakeRepository(replay)), MemoryPolicy(), embedding
    )

    assert await service.create_episode(draft) == replay
    assert provider.inputs == []
