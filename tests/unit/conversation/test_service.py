import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import uuid7

from eva_ai.actions.canonical import CanonicalEmail
from eva_ai.actions.contracts import ActionProposalPreparer
from eva_ai.actions.types import (
    ActionProposalPreparation,
    ActionRevisionContext,
    DraftRevisionCandidate,
    RevisionLookup,
    RevisionLookupStatus,
)
from eva_ai.agent.types import AgentUsage, ProposedAction
from eva_ai.conversation.repository import (
    ConversationRepository,
    ConversationTurnClaim,
    ConversationTurnSubject,
)
from eva_ai.conversation.service import ConversationService
from eva_ai.conversation.types import (
    ConversationAgentResult,
    ConversationInvocationResult,
    ConversationOutcome,
    DraftRevisionInvocationResult,
    DraftRevisionRequest,
)
from eva_ai.memory.types import AgentWorkingContext, ContextIdentity, ContextSituation
from eva_ai.situations.types import AttentionLevel
from eva_ai.telegram.contracts import TelegramGateway


class Telegram:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.actions: list[tuple[int, str]] = []
        self.called = asyncio.Event()

    async def send_chat_action(self, *, chat_id: int, action: str) -> None:
        self.actions.append((chat_id, action))
        self.called.set()
        if self.fail:
            raise RuntimeError("provider unavailable")


class ProposalPreparer:
    def __init__(self) -> None:
        self.values: dict[str, object] | None = None

    async def prepare_model_proposals(
        self, proposals: tuple[ProposedAction, ...], **values: object
    ) -> ActionProposalPreparation:
        self.values = {"proposals": proposals, **values}
        return ActionProposalPreparation(clarification="Which address should I use?")


async def test_conversation_prepares_reactive_action_with_scoped_subject() -> None:
    preparer = ProposalPreparer()
    service = object.__new__(ConversationService)
    service._action_proposals = cast(ActionProposalPreparer, preparer)
    proposal = ProposedAction(
        capability="gmail.create_draft",
        description="Create a draft",
        arguments={},
    )
    invocation = ConversationInvocationResult(
        result=ConversationAgentResult(
            message="I can draft that.",
            reasoning_summary="The user requested a draft.",
            proposed_actions=(proposal,),
        ),
        usage=AgentUsage(),
    )
    subject = cast(
        ConversationTurnSubject,
        SimpleNamespace(
            gmail_thread_id="thread-1",
            connector_identity="owner@example.com",
        ),
    )

    result = await service._prepare_actions(invocation, None, subject)

    assert result.clarification == "Which address should I use?"
    assert preparer.values == {
        "proposals": (proposal,),
        "reader": None,
        "allowed_thread_id": "thread-1",
        "account_identity": "owner@example.com",
    }


async def test_typing_refresh_starts_immediately_and_repeats() -> None:
    telegram = Telegram()
    service = object.__new__(ConversationService)
    service._telegram = cast(TelegramGateway, telegram)
    service._typing_refresh_seconds = 0.001

    task = asyncio.create_task(service._refresh_typing(42))
    await asyncio.wait_for(telegram.called.wait(), timeout=1)
    await asyncio.sleep(0.003)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert len(telegram.actions) >= 2
    assert set(telegram.actions) == {(42, "typing")}


async def test_typing_refresh_ignores_provider_failure() -> None:
    telegram = Telegram(fail=True)
    service = object.__new__(ConversationService)
    service._telegram = cast(TelegramGateway, telegram)
    service._typing_refresh_seconds = 60

    task = asyncio.create_task(service._refresh_typing(42))
    await asyncio.wait_for(telegram.called.wait(), timeout=1)
    assert task.done() is False
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_exhausted_retry_completes_with_user_visible_fallback() -> None:
    class Conversations:
        def __init__(self) -> None:
            self.completed: dict[str, object] | None = None

        async def complete(self, claim: ConversationTurnClaim, **values: object) -> None:
            self.completed = values

        async def fail(self, claim: ConversationTurnClaim, **values: object) -> None:
            raise AssertionError("exhausted retries must not silently fail the turn")

    conversations = Conversations()
    service = object.__new__(ConversationService)
    service._conversations = cast(ConversationRepository, conversations)
    service._max_attempts = 1
    service._agent_version = "conversation-v2"
    service._clock = lambda: datetime(2030, 1, 1, tzinfo=UTC)
    claim = ConversationTurnClaim(
        turn_id=uuid7(),
        conversation_id=uuid7(),
        claim_id=uuid7(),
        event_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        attempt_count=1,
    )

    outcome = await service._retry(claim, "MODEL_OUTPUT_INVALID")

    assert outcome is ConversationOutcome.SUCCEEDED
    assert conversations.completed is not None
    assert "try once more" in str(conversations.completed["response_text"])
    assert conversations.completed["provider_response_id"] is None


async def test_active_revision_routes_exact_draft_and_instruction_to_revision_agent() -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    ids = [uuid7() for _ in range(8)]
    user_id, workspace_id, situation_id, conversation_id = ids[:4]
    session_id, managed_draft_id, send_proposal_id, telegram_account_id = ids[4:]
    current = CanonicalEmail(
        mode="NEW",
        to=("old@example.com",),
        subject="Original",
        text_body="Original body",
    )

    class Revisions:
        def __init__(self) -> None:
            self.completed: dict[str, object] | None = None

        async def load(self, **values: object) -> RevisionLookup:
            assert values["telegram_account_id"] == telegram_account_id
            return RevisionLookup(
                status=RevisionLookupStatus.ACTIVE,
                context=ActionRevisionContext(
                    session_id=session_id,
                    conversation_id=conversation_id,
                    managed_draft_id=managed_draft_id,
                    active_send_proposal_id=send_proposal_id,
                    current_message=current,
                    expires_at=now + timedelta(minutes=15),
                ),
            )

        async def complete(self, **values: object) -> object:
            self.completed = values
            return object()

    class RevisionAgent:
        def __init__(self) -> None:
            self.request: DraftRevisionRequest | None = None

        async def revise(self, request: DraftRevisionRequest) -> DraftRevisionInvocationResult:
            self.request = request
            return DraftRevisionInvocationResult(
                candidate=DraftRevisionCandidate(
                    mode="NEW",
                    to=("new@example.com",),
                    subject="Revised",
                    text_body="Revised body",
                ),
                reasoning_summary="Applied the requested changes.",
            )

    revisions = Revisions()
    revision_agent = RevisionAgent()
    service = object.__new__(ConversationService)
    service._action_revisions = revisions
    service._revision_agent = revision_agent
    service._clock = lambda: now
    claim = ConversationTurnClaim(
        turn_id=uuid7(),
        conversation_id=conversation_id,
        claim_id=uuid7(),
        event_id=uuid7(),
        user_id=user_id,
        workspace_id=workspace_id,
        attempt_count=1,
    )
    subject = cast(
        ConversationTurnSubject,
        SimpleNamespace(
            telegram_account_id=telegram_account_id,
            turn=SimpleNamespace(text="Make it warmer", user_id=user_id, workspace_id=workspace_id),
            history=(),
        ),
    )
    context = AgentWorkingContext(
        user_id=user_id,
        workspace_id=workspace_id,
        identity=ContextIdentity(display_name="Saswat", workspace_name="Personal"),
        situation=ContextSituation(
            id=situation_id,
            title="Draft",
            summary="",
            current_state="DRAFTING",
            next_action=None,
            next_expected=None,
            attention=AttentionLevel.NORMAL,
            last_activity_at=now,
        ),
        goals=(),
        facts=(),
        episodes=(),
        built_at=now,
        digest="a" * 64,
    )

    status, invocation = await service._process_revision(claim, subject, context)

    assert status is RevisionLookupStatus.ACTIVE
    assert invocation is not None
    assert revision_agent.request is not None
    assert revision_agent.request.current_message == current
    assert revision_agent.request.instruction == "Make it warmer"
    assert revisions.completed is not None
    assert revisions.completed["session_id"] == session_id
    assert revisions.completed["conversation_turn_id"] == claim.turn_id


def test_expired_revision_explains_normal_conversation_fallback() -> None:
    invocation = ConversationInvocationResult(
        result=ConversationAgentResult(
            message="Here is the normal answer.",
            reasoning_summary="Answered normally.",
        )
    )

    response = ConversationService._normal_response_text(
        invocation,
        ActionProposalPreparation(),
        RevisionLookupStatus.EXPIRED,
    )

    assert response == (
        "That draft revision window expired, so I treated this as a normal message. "
        "Here is the normal answer."
    )
