from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import uuid7

from eva_ai.memory.learning import MemoryLearningService
from eva_ai.memory.service import MemoryService
from eva_ai.memory.types import (
    EpisodicMemoryDraft,
    MemoryEpisodeType,
    MemoryFactDraft,
    MemoryProposal,
    MemoryProposalKind,
    MemoryScopeType,
    MemorySourceType,
)

NOW = datetime(2026, 9, 7, tzinfo=UTC)


class Memories:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.facts: list[MemoryFactDraft] = []
        self.episodes: list[EpisodicMemoryDraft] = []

    async def put_fact(self, draft: MemoryFactDraft) -> object:
        if self.fail:
            raise ValueError("policy rejected")
        self.facts.append(draft)
        return object()

    async def create_episode(self, draft: EpisodicMemoryDraft) -> object:
        if self.fail:
            raise ValueError("embedding unavailable")
        self.episodes.append(draft)
        return object()


async def test_learning_materializes_proposals_inside_authenticated_workspace() -> None:
    user_id, workspace_id, situation_id, untrusted_scope_id = (
        uuid7(),
        uuid7(),
        uuid7(),
        uuid7(),
    )
    memories = Memories()
    learner = MemoryLearningService(cast(MemoryService, memories))
    proposals = (
        MemoryProposal(
            kind=MemoryProposalKind.FACT,
            claim="Prefers direct, concise updates",
            namespace="preferences",
            key="communication_style",
            scope_type=MemoryScopeType.SITUATION,
            scope_id=untrusted_scope_id,
            source_type=MemorySourceType.AGENT_INFERRED,
            source_ref="untrusted:model-value",
            confidence=Decimal("0.9"),
            reason="The user stated this preference.",
        ),
        MemoryProposal(
            kind=MemoryProposalKind.EPISODE,
            claim="The user chose to prepare for the Scaler interview.",
            source_type=MemorySourceType.AGENT_INFERRED,
            source_ref="untrusted:model-value",
            confidence=Decimal("0.85"),
            reason="This is a meaningful commitment.",
        ),
    )

    outcome = await learner.learn(
        proposals,
        user_id=user_id,
        workspace_id=workspace_id,
        situation_id=situation_id,
        goal_ids=(),
        source_type=MemorySourceType.AGENT_INFERRED,
        source_ref="telegram-conversation:turn-1",
        occurred_at=NOW,
        episode_type=MemoryEpisodeType.EXPERIENCE,
    )

    assert outcome.stored == 2
    assert memories.facts[0].scope_type is MemoryScopeType.WORKSPACE
    assert memories.facts[0].scope_id == workspace_id
    assert memories.facts[0].source_ref == "telegram-conversation:turn-1"
    assert memories.episodes[0].situation_id == situation_id
    assert memories.episodes[0].type is MemoryEpisodeType.EXPERIENCE


async def test_learning_skips_low_confidence_and_isolates_write_failure() -> None:
    user_id, workspace_id, situation_id = uuid7(), uuid7(), uuid7()
    memories = Memories(fail=True)
    learner = MemoryLearningService(cast(MemoryService, memories))
    proposals = (
        MemoryProposal(
            kind=MemoryProposalKind.EPISODE,
            claim="A speculative event",
            source_type=MemorySourceType.AGENT_INFERRED,
            source_ref="model",
            confidence=Decimal("0.5"),
            reason="Uncertain.",
        ),
        MemoryProposal(
            kind=MemoryProposalKind.EPISODE,
            claim="A durable event",
            source_type=MemorySourceType.AGENT_INFERRED,
            source_ref="model",
            confidence=Decimal("0.9"),
            reason="Relevant later.",
        ),
    )

    outcome = await learner.learn(
        proposals,
        user_id=user_id,
        workspace_id=workspace_id,
        situation_id=situation_id,
        goal_ids=(),
        source_type=MemorySourceType.AGENT_INFERRED,
        source_ref="telegram-conversation:turn-2",
        occurred_at=NOW,
        episode_type=MemoryEpisodeType.EXPERIENCE,
    )

    assert outcome.proposed == 2
    assert outcome.stored == 0
    assert outcome.skipped == 2
