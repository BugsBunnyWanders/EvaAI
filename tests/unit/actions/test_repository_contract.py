from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from eva_ai.actions.canonical import CanonicalEmail
from eva_ai.actions.types import (
    ActionOrigin,
    ActionTaskRequest,
    GmailActionCapability,
    NewActionProposal,
    PolicyDecision,
)


def test_new_proposal_derives_hash_from_canonical_message() -> None:
    proposal = NewActionProposal(
        user_id=uuid4(),
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        event_id=uuid4(),
        origin=ActionOrigin.TELEGRAM_USER,
        capability=GmailActionCapability.CREATE_DRAFT,
        message=CanonicalEmail(
            mode="NEW",
            to=("person@example.com",),
            subject="Subject",
            text_body="Body",
        ),
        description="Create a draft",
        policy_decision=PolicyDecision.ALLOW,
        source_key="turn:1:action:0",
        proposal_family_key="turn:1:action:0",
        created_at=datetime.now(UTC),
    )

    assert proposal.parameters_hash == proposal.message_hash
    assert len(proposal.parameters_hash) == 64


def test_new_proposal_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        NewActionProposal(
            user_id=uuid4(),
            workspace_id=uuid4(),
            connector_account_id=uuid4(),
            event_id=uuid4(),
            origin=ActionOrigin.TELEGRAM_USER,
            capability=GmailActionCapability.CREATE_DRAFT,
            message=CanonicalEmail(
                mode="NEW",
                to=("person@example.com",),
                subject="Subject",
                text_body="Body",
            ),
            description="Create a draft",
            policy_decision=PolicyDecision.ALLOW,
            source_key="turn:1:action:0",
            proposal_family_key="turn:1:action:0",
            created_at=datetime(2026, 1, 1),
        )


def test_task_request_rejects_scope_or_email_content() -> None:
    with pytest.raises(ValidationError):
        ActionTaskRequest.model_validate(
            {
                "action_id": str(uuid4()),
                "schema_version": 1,
                "subject": "must not leave the database",
            }
        )
