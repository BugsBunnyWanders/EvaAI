import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

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

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryLearningOutcome:
    proposed: int
    stored: int
    skipped: int


class MemoryLearningService:
    """Materialize bounded agent proposals without trusting model-supplied tenant scope."""

    def __init__(self, memories: MemoryService, *, minimum_confidence: Decimal = Decimal("0.75")):
        self._memories = memories
        self._minimum_confidence = minimum_confidence

    async def learn(
        self,
        proposals: tuple[MemoryProposal, ...],
        *,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID,
        goal_ids: tuple[UUID, ...],
        source_type: MemorySourceType,
        source_ref: str,
        occurred_at: datetime,
        episode_type: MemoryEpisodeType,
    ) -> MemoryLearningOutcome:
        if source_type not in {
            MemorySourceType.AGENT_INFERRED,
            MemorySourceType.EXTERNAL_EVENT,
        }:
            raise ValueError("automatic learning requires agent or external-event provenance")

        stored = 0
        skipped = 0
        origin_digest = hashlib.sha256(source_ref.encode("utf-8")).hexdigest()[:24]
        for index, proposal in enumerate(proposals):
            if proposal.confidence < self._minimum_confidence:
                skipped += 1
                continue
            try:
                if proposal.kind is MemoryProposalKind.FACT:
                    assert proposal.namespace is not None
                    assert proposal.key is not None
                    await self._memories.put_fact(
                        MemoryFactDraft(
                            user_id=user_id,
                            workspace_id=workspace_id,
                            namespace=proposal.namespace,
                            key=proposal.key,
                            value_json=proposal.claim,
                            # Automatic learning is deliberately workspace-scoped. Model-provided
                            # scope IDs are never trusted as authorization boundaries.
                            scope_type=MemoryScopeType.WORKSPACE,
                            scope_id=workspace_id,
                            source_type=source_type,
                            source_ref=source_ref,
                            confidence=proposal.confidence,
                            idempotency_key=f"auto-fact:{origin_digest}:{index}",
                            valid_from=occurred_at,
                        )
                    )
                else:
                    await self._memories.create_episode(
                        EpisodicMemoryDraft(
                            user_id=user_id,
                            workspace_id=workspace_id,
                            type=episode_type,
                            summary=proposal.claim,
                            goal_ids=goal_ids,
                            situation_id=situation_id,
                            importance=proposal.confidence,
                            confidence=proposal.confidence,
                            source_type=source_type,
                            source_ref=source_ref,
                            idempotency_key=f"auto-episode:{origin_digest}:{index}",
                            occurred_at=occurred_at,
                        )
                    )
            except Exception:
                # Learning is additive. Policy, provider, or concurrent-write failures must never
                # suppress the answer or notification that prompted the proposal.
                logger.warning(
                    "memory proposal skipped",
                    extra={"kind": proposal.kind.value, "proposal_index": index},
                )
                skipped += 1
            else:
                stored += 1
        return MemoryLearningOutcome(
            proposed=len(proposals),
            stored=stored,
            skipped=skipped,
        )
