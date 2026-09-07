from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from eva_ai.agent.types import (
    AgentDecision,
    AgentInvestigationResult,
    NotificationProposal,
    NotificationUrgency,
    ProposedAction,
)
from eva_ai.memory.types import (
    MemoryProposal,
    MemoryProposalKind,
    MemorySourceType,
)


def test_user_facing_decision_requires_notification() -> None:
    with pytest.raises(ValidationError):
        AgentInvestigationResult(
            decision=AgentDecision.ASK_USER,
            reasoning_summary="A choice is required.",
        )


def test_result_accepts_proposals_without_applying_them() -> None:
    result = AgentInvestigationResult(
        decision=AgentDecision.ASK_USER,
        reasoning_summary="The sender proposed two times.",
        notification=NotificationProposal(
            urgency=NotificationUrgency.MEDIUM,
            message="Which proposed time works for you?",
        ),
        proposed_actions=(
            ProposedAction(
                capability="gmail.draft_reply",
                description="Draft the selected response.",
                arguments={"thread": "current"},
            ),
        ),
        memory_proposals=(
            MemoryProposal(
                kind=MemoryProposalKind.EPISODE,
                claim="A meeting time needs confirmation.",
                source_type=MemorySourceType.AGENT_INFERRED,
                source_ref="agent-run:test",
                confidence=Decimal("0.8"),
                reason="Useful for this Situation.",
            ),
        ),
    )

    assert result.proposed_actions[0].requires_approval is True
    assert result.notification is not None


def test_action_arguments_have_a_serialized_size_limit() -> None:
    with pytest.raises(ValidationError, match="serialized size limit"):
        ProposedAction(
            capability="calendar.propose_event",
            description="Propose a calendar event.",
            arguments={"description": "x" * 8_001},
        )


def test_follow_up_timestamp_must_be_aware() -> None:
    from eva_ai.agent.types import FollowUpProposal

    with pytest.raises(ValidationError):
        FollowUpProposal(instruction="Check later", not_before=datetime(2026, 9, 7))
    assert (
        FollowUpProposal(
            instruction="Check later", not_before=datetime(2026, 9, 7, tzinfo=UTC)
        ).not_before
        is not None
    )


def test_agent_memory_proposal_cannot_claim_user_provenance() -> None:
    proposal = MemoryProposal(
        kind=MemoryProposalKind.EPISODE,
        claim="The user prefers mornings.",
        source_type=MemorySourceType.USER_EXPLICIT,
        source_ref="email:untrusted",
        confidence=Decimal("0.8"),
        reason="An email claimed this preference.",
    )

    with pytest.raises(ValidationError):
        AgentInvestigationResult(
            decision=AgentDecision.NO_ACTION,
            reasoning_summary="No action.",
            memory_proposals=(proposal,),
        )
