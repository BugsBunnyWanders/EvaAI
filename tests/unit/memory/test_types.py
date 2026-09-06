import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from eva_ai.memory.types import (
    EpisodicMemoryDraft,
    MemoryEpisodeType,
    MemoryFactDraft,
    MemoryProposal,
    MemoryProposalKind,
    MemoryScopeType,
    MemorySourceType,
)

USER_ID = UUID("00000000-0000-7000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000002")
GOAL_ID = UUID("00000000-0000-7000-8000-000000000003")
NOW = datetime(2026, 9, 6, tzinfo=UTC)


def fact_values() -> dict[str, object]:
    return {
        "user_id": USER_ID,
        "workspace_id": WORKSPACE_ID,
        "namespace": "  Career.Preferences ",
        "key": " Interview_Time ",
        "value_json": {"period": "afternoon"},
        "scope_type": MemoryScopeType.WORKSPACE,
        "scope_id": WORKSPACE_ID,
        "source_type": MemorySourceType.USER_EXPLICIT,
        "source_ref": "  operator:initial-profile ",
        "confidence": Decimal("1"),
        "idempotency_key": " profile:interview-time:v1 ",
        "valid_from": NOW,
    }


def test_fact_draft_normalizes_identity_and_provenance() -> None:
    draft = MemoryFactDraft(**fact_values())

    assert draft.namespace == "career.preferences"
    assert draft.key == "interview_time"
    assert draft.source_ref == "operator:initial-profile"
    assert draft.idempotency_key == "profile:interview-time:v1"


def test_fact_draft_requires_workspace_scope_id_to_match() -> None:
    with pytest.raises(ValidationError, match="Workspace scope"):
        MemoryFactDraft(**{**fact_values(), "scope_id": GOAL_ID})


def test_fact_draft_rejects_naive_or_reversed_validity() -> None:
    with pytest.raises(ValidationError):
        MemoryFactDraft(**{**fact_values(), "valid_from": datetime(2026, 9, 6)})
    with pytest.raises(ValidationError, match="valid_until"):
        MemoryFactDraft(**{**fact_values(), "valid_until": NOW})


def test_fact_value_has_compact_eight_kibibyte_limit() -> None:
    within_limit = {"note": "x" * 8181}
    assert len(json.dumps(within_limit, separators=(",", ":")).encode()) == 8192
    assert MemoryFactDraft(**{**fact_values(), "value_json": within_limit})

    with pytest.raises(ValidationError, match="8 KiB"):
        MemoryFactDraft(**{**fact_values(), "value_json": {"note": "x" * 8182}})


def test_episode_normalizes_entities_and_goal_ids() -> None:
    other_goal = UUID("00000000-0000-7000-8000-000000000004")
    draft = EpisodicMemoryDraft(
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        type=MemoryEpisodeType.DECISION,
        summary="  User preferred the infrastructure-focused role. ",
        entities=(" Acme ", "acme", "Platform Team"),
        goal_ids=(other_goal, GOAL_ID, GOAL_ID),
        situation_id=None,
        importance=Decimal("0.8"),
        confidence=Decimal("1"),
        source_type=MemorySourceType.USER_EXPLICIT,
        source_ref="operator:decision",
        idempotency_key="decision:acme:v1",
        occurred_at=NOW,
    )

    assert draft.summary == "User preferred the infrastructure-focused role."
    assert draft.entities == ("acme", "platform team")
    assert draft.goal_ids == (GOAL_ID, other_goal)


def test_episode_rejects_naive_time_and_excessive_summary() -> None:
    values = {
        "user_id": USER_ID,
        "workspace_id": WORKSPACE_ID,
        "type": MemoryEpisodeType.EXPERIENCE,
        "summary": "x" * 4001,
        "importance": Decimal("0.5"),
        "confidence": Decimal("0.5"),
        "source_type": MemorySourceType.SYSTEM_OBSERVED,
        "source_ref": "system:test",
        "idempotency_key": "episode:test",
        "occurred_at": datetime(2026, 9, 6),
    }
    with pytest.raises(ValidationError):
        EpisodicMemoryDraft(**values)


def test_fact_proposal_requires_a_complete_normalized_slot() -> None:
    with pytest.raises(ValidationError, match="require namespace"):
        MemoryProposal(
            kind=MemoryProposalKind.FACT,
            claim="The user prefers concise updates.",
            source_type=MemorySourceType.AGENT_INFERRED,
            source_ref="conversation:42",
            confidence=Decimal("0.8"),
            reason="The user stated this directly.",
        )

    proposal = MemoryProposal(
        kind=MemoryProposalKind.FACT,
        claim="The user prefers concise updates.",
        namespace=" Preferences ",
        key=" Communication.Style ",
        scope_type=MemoryScopeType.WORKSPACE,
        scope_id=WORKSPACE_ID,
        source_type=MemorySourceType.AGENT_INFERRED,
        source_ref="conversation:42",
        confidence=Decimal("0.8"),
        reason="The user stated this directly.",
    )

    assert (proposal.namespace, proposal.key) == ("preferences", "communication.style")


def test_episode_proposal_rejects_fact_slot_fields() -> None:
    with pytest.raises(ValidationError, match="cannot contain fact slot"):
        MemoryProposal(
            kind=MemoryProposalKind.EPISODE,
            claim="A decision was made.",
            namespace="decision",
            source_type=MemorySourceType.SYSTEM_OBSERVED,
            source_ref="situation:42",
            confidence=Decimal("1"),
            reason="Situation transitioned.",
        )
