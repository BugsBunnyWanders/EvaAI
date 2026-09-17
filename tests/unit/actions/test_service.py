from datetime import UTC, datetime
from typing import cast
from uuid import uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from eva_ai.actions.service import ActionProposalService
from eva_ai.actions.types import (
    ActionOrigin,
    ActionProposalPreparation,
    AllowedActionCreation,
    GmailActionCapability,
    NewActionProposal,
    PolicyDecision,
)
from eva_ai.agent.contracts import GmailInvestigationReader
from eva_ai.agent.types import (
    GmailMessageEvidence,
    GmailSearchEvidence,
    GmailThreadEvidence,
    ProposedAction,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)


class Reader:
    def __init__(
        self,
        *,
        search_messages: tuple[GmailMessageEvidence, ...] = (),
        thread: GmailThreadEvidence | None = None,
    ) -> None:
        self.search_messages = search_messages
        self.thread = thread
        self.queries: list[str] = []

    async def search(self, query: str) -> GmailSearchEvidence:
        self.queries.append(query)
        return GmailSearchEvidence(messages=self.search_messages, truncated=False)

    async def read_thread(self) -> GmailThreadEvidence:
        if self.thread is None:
            raise AssertionError("thread read was not expected")
        return self.thread


class Store:
    def __init__(self) -> None:
        self.calls: list[tuple[NewActionProposal, str]] = []

    async def create_allowed_action_in_session(
        self,
        session: AsyncSession,
        *,
        proposal: NewActionProposal,
        destination: str,
    ) -> AllowedActionCreation:
        del session
        self.calls.append((proposal, destination))
        return cast(AllowedActionCreation, object())


def _proposal(**arguments: object) -> ProposedAction:
    values: dict[str, object] = {
        "mode": "NEW",
        "to": ["person@example.com"],
        "subject": "Hello",
        "text_body": "A useful message.",
    }
    values.update(arguments)
    return ProposedAction(
        capability="gmail.create_draft",
        description="Create a useful draft",
        arguments=values,
        requires_approval=False,
    )


def _evidence(sender: str, recipients: str = "owner@example.com") -> GmailMessageEvidence:
    return GmailMessageEvidence(
        message_id=str(uuid7()),
        thread_id=str(uuid7()),
        rfc_message_id=f"<{uuid7()}@example.com>",
        internal_date=NOW,
        sender=sender,
        recipients=recipients,
        subject="Prior correspondence",
        snippet="",
        plain_text="",
    )


@pytest.mark.asyncio
async def test_explicit_address_prepares_canonical_create_draft() -> None:
    service = ActionProposalService(Store(), destination="eva-actions")

    result = await service.prepare_model_proposals(
        (_proposal(),),
        reader=cast(GmailInvestigationReader, Reader()),
        allowed_thread_id=None,
        account_identity="owner@example.com",
    )

    assert isinstance(result, ActionProposalPreparation)
    assert result.clarification is None
    assert len(result.proposals) == 1
    assert result.proposals[0].capability is GmailActionCapability.CREATE_DRAFT
    assert result.proposals[0].message.to == ("person@example.com",)


@pytest.mark.asyncio
async def test_one_bounded_history_match_resolves_named_recipient() -> None:
    reader = Reader(search_messages=(_evidence("Jane Doe <jane@example.com>"),))
    service = ActionProposalService(Store(), destination="eva-actions")

    result = await service.prepare_model_proposals(
        (_proposal(to=["Jane Doe"]),),
        reader=cast(GmailInvestigationReader, reader),
        allowed_thread_id=None,
        account_identity="owner@example.com",
    )

    assert result.clarification is None
    assert result.proposals[0].message.to == ("jane@example.com",)
    assert len(reader.queries) == 1
    assert "Jane Doe" in reader.queries[0]


@pytest.mark.asyncio
async def test_reply_uses_trusted_thread_subject_and_rfc_message_ids() -> None:
    thread = GmailThreadEvidence(
        thread_id="trusted-thread",
        messages=(
            _evidence("person@example.com").model_copy(
                update={
                    "thread_id": "trusted-thread",
                    "rfc_message_id": "<first@example.com>",
                    "subject": "Meeting details",
                }
            ),
            _evidence("owner@example.com", "person@example.com").model_copy(
                update={
                    "thread_id": "trusted-thread",
                    "rfc_message_id": "<second@example.com>",
                    "subject": "Meeting details",
                }
            ),
        ),
        truncated=False,
    )
    service = ActionProposalService(Store(), destination="eva-actions")

    result = await service.prepare_model_proposals(
        (
            _proposal(
                mode="REPLY",
                thread_id="trusted-thread",
                subject="Model-provided subject is not trusted",
            ),
        ),
        reader=cast(GmailInvestigationReader, Reader(thread=thread)),
        allowed_thread_id="trusted-thread",
        account_identity="owner@example.com",
    )

    message = result.proposals[0].message
    assert message.thread_id == "trusted-thread"
    assert message.subject == "Re: Meeting details"
    assert message.in_reply_to == "<second@example.com>"
    assert message.references == ("<first@example.com>", "<second@example.com>")


@pytest.mark.asyncio
async def test_reply_rejects_untrusted_message_ids() -> None:
    thread = GmailThreadEvidence(
        thread_id="trusted-thread",
        messages=(
            _evidence("person@example.com").model_copy(
                update={
                    "thread_id": "trusted-thread",
                    "rfc_message_id": "not-a-message-id",
                    "subject": "Meeting details",
                }
            ),
        ),
        truncated=False,
    )
    service = ActionProposalService(Store(), destination="eva-actions")

    result = await service.prepare_model_proposals(
        (_proposal(mode="REPLY", thread_id="trusted-thread"),),
        reader=cast(GmailInvestigationReader, Reader(thread=thread)),
        allowed_thread_id="trusted-thread",
        account_identity="owner@example.com",
    )

    assert result.proposals == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages",
    [
        (),
        (
            _evidence("Jane Doe <jane.one@example.com>"),
            _evidence("Jane Doe <jane.two@example.com>"),
        ),
    ],
)
async def test_unresolved_or_ambiguous_name_returns_clarification_without_proposal(
    messages: tuple[GmailMessageEvidence, ...],
) -> None:
    service = ActionProposalService(Store(), destination="eva-actions")

    result = await service.prepare_model_proposals(
        (_proposal(to=["Jane Doe"]),),
        reader=cast(GmailInvestigationReader, Reader(search_messages=messages)),
        allowed_thread_id=None,
        account_identity="owner@example.com",
    )

    assert result.proposals == ()
    assert result.clarification is not None


@pytest.mark.asyncio
async def test_writer_uses_only_trusted_scope_and_provenance() -> None:
    store = Store()
    service = ActionProposalService(store, destination="eva-actions")
    preparation = await service.prepare_model_proposals(
        (
            _proposal(
                user_id=str(uuid7()),
                workspace_id=str(uuid7()),
                connector_account_id=str(uuid7()),
                event_id=str(uuid7()),
                agent_run_id=str(uuid7()),
                origin="SYSTEM",
            ),
        ),
        reader=cast(GmailInvestigationReader, Reader()),
        allowed_thread_id=None,
        account_identity="owner@example.com",
    )
    trusted = {
        "user_id": uuid7(),
        "workspace_id": uuid7(),
        "connector_account_id": uuid7(),
        "event_id": uuid7(),
        "situation_id": uuid7(),
        "agent_run_id": uuid7(),
    }

    await service.create_from_model_proposals_in_session(
        cast(AsyncSession, object()),
        proposals=preparation.proposals,
        **trusted,
        conversation_turn_id=None,
        origin=ActionOrigin.AGENT_RUN,
        source_prefix="agent-run:trusted",
        created_at=NOW,
    )

    assert len(store.calls) == 1
    persisted, destination = store.calls[0]
    assert persisted.user_id == trusted["user_id"]
    assert persisted.workspace_id == trusted["workspace_id"]
    assert persisted.connector_account_id == trusted["connector_account_id"]
    assert persisted.event_id == trusted["event_id"]
    assert persisted.agent_run_id == trusted["agent_run_id"]
    assert persisted.origin is ActionOrigin.AGENT_RUN
    assert persisted.policy_decision is PolicyDecision.ALLOW
    assert destination == "eva-actions"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proposal,allowed_thread_id",
    [
        (
            ProposedAction(
                capability="gmail.send_draft",
                description="Bypass approval",
                arguments={},
            ),
            None,
        ),
        (_proposal(attachments=["payload"]), None),
        (
            _proposal(
                mode="REPLY",
                thread_id="other-thread",
                to=["Jane Doe"],
                in_reply_to="<untrusted@example.com>",
            ),
            "trusted-thread",
        ),
        (_proposal(to=[]), None),
        (_proposal(to=["   "]), None),
    ],
)
async def test_invalid_model_action_is_discarded_without_provider_or_persistence(
    proposal: ProposedAction,
    allowed_thread_id: str | None,
) -> None:
    reader = Reader()
    service = ActionProposalService(Store(), destination="eva-actions")

    result = await service.prepare_model_proposals(
        (proposal,),
        reader=cast(GmailInvestigationReader, reader),
        allowed_thread_id=allowed_thread_id,
        account_identity="owner@example.com",
    )

    assert result.proposals == ()
    assert reader.queries == []
