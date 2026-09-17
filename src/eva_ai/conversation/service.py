import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from eva_ai.actions.contracts import ActionProposalPreparer
from eva_ai.actions.errors import ActionScopeError, ActionValidationError
from eva_ai.actions.service import ActionRevisionService
from eva_ai.actions.types import (
    ActionProposalPreparation,
    RevisionLookupStatus,
)
from eva_ai.agent.errors import AgentPermanentError, AgentToolBudgetExceeded
from eva_ai.agent.gmail import ScopedGmailInvestigationReader
from eva_ai.agent.types import AgentUsage
from eva_ai.connectors.gmail.contracts import (
    AuthorizationRevoked,
    CredentialStore,
    GmailClientFactory,
    use_gmail_client,
)
from eva_ai.conversation.contracts import ConversationAgent, DraftRevisionAgent
from eva_ai.conversation.errors import (
    ConversationModelOutputError,
    ConversationPermanentError,
    ConversationScopeError,
    ConversationTransientError,
)
from eva_ai.conversation.repository import (
    ConversationRepository,
    ConversationTurnClaim,
    ConversationTurnSubject,
)
from eva_ai.conversation.types import (
    ConversationAgentRequest,
    ConversationInvocationResult,
    ConversationOutcome,
    ConversationTurnRecord,
    ConversationTurnStatus,
    DraftRevisionInvocationResult,
    DraftRevisionRequest,
)
from eva_ai.integrations.gcp.secret_manager import SecretManagerProviderError
from eva_ai.integrations.gmail.api import GmailProviderError, InvalidAuthorizedUserCredentials
from eva_ai.memory.context import MemoryContextBuilder
from eva_ai.memory.errors import MemoryNotFoundError
from eva_ai.memory.learning import MemoryLearningService
from eva_ai.memory.types import AgentWorkingContext, MemoryEpisodeType, MemorySourceType
from eva_ai.situations.types import SituationType
from eva_ai.telegram.contracts import TelegramGateway
from eva_ai.telegram.types import TelegramTurnRequestedMessage


class ConversationService:
    def __init__(
        self,
        *,
        conversations: ConversationRepository,
        context_builder: MemoryContextBuilder,
        credential_store: CredentialStore,
        gmail_clients: GmailClientFactory,
        agent: ConversationAgent,
        agent_version: str,
        lease_seconds: int,
        max_attempts: int,
        retry_initial_backoff_seconds: float,
        retry_max_backoff_seconds: float,
        history_turn_limit: int,
        history_max_chars: int,
        thread_message_limit: int,
        search_result_limit: int,
        body_max_chars: int,
        memory_learner: MemoryLearningService | None = None,
        action_proposals: ActionProposalPreparer | None = None,
        action_revisions: ActionRevisionService | None = None,
        revision_agent: DraftRevisionAgent | None = None,
        telegram: TelegramGateway | None = None,
        typing_refresh_seconds: float = 4.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._conversations = conversations
        self._context_builder = context_builder
        self._credential_store = credential_store
        self._gmail_clients = gmail_clients
        self._agent = agent
        self._agent_version = agent_version
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._retry_initial_backoff_seconds = retry_initial_backoff_seconds
        self._retry_max_backoff_seconds = retry_max_backoff_seconds
        self._history_turn_limit = history_turn_limit
        self._history_max_chars = history_max_chars
        self._thread_message_limit = thread_message_limit
        self._search_result_limit = search_result_limit
        self._body_max_chars = body_max_chars
        self._memory_learner = memory_learner
        self._action_proposals = action_proposals
        self._action_revisions = action_revisions
        self._revision_agent = revision_agent
        self._telegram = telegram
        self._typing_refresh_seconds = typing_refresh_seconds
        self._clock = clock

    async def process(self, message: TelegramTurnRequestedMessage) -> ConversationOutcome:
        event_type = await self._conversations.event_type(message)
        if event_type == "telegram.callback.received":
            await self._conversations.mark_callback_handled(message, processed_at=self._clock())
            return ConversationOutcome.TERMINAL
        if event_type != "telegram.message.received":
            return ConversationOutcome.TERMINAL

        claim = await self._conversations.resolve_and_claim(
            message,
            now=self._clock(),
            lease_seconds=self._lease_seconds,
            agent_version=self._agent_version,
        )
        if claim is None:
            turn = await self._conversations.find_turn_for_event(
                event_id=message.event_id,
                user_id=message.user_id,
                workspace_id=message.workspace_id,
            )
            if turn is None or turn.status in {
                ConversationTurnStatus.SUCCEEDED,
                ConversationTurnStatus.PERMANENT_FAILURE,
            }:
                return ConversationOutcome.TERMINAL
            return ConversationOutcome.DEFERRED
        typing_task: asyncio.Task[None] | None = None
        try:
            subject = await self._conversations.load_subject(
                claim,
                history_limit=self._history_turn_limit,
                history_max_chars=self._history_max_chars,
            )
            if self._telegram is not None:
                typing_task = asyncio.create_task(
                    self._refresh_typing(subject.telegram_chat_id),
                    name=f"telegram-typing-{claim.turn_id}",
                )
            context = await self._context_builder.build_for_situation(
                user_id=claim.user_id,
                workspace_id=claim.workspace_id,
                situation_id=subject.conversation.situation_id,
                focus=subject.turn.text,
            )
            revision_status, revision_invocation = await self._process_revision(
                claim,
                subject,
                context,
            )
            if revision_invocation is not None:
                await self._conversations.complete(
                    claim,
                    response_text=(
                        "I’m updating that draft now. I’ll show you the complete revised version "
                        "for approval as soon as it’s ready."
                    ),
                    agent_version=self._agent_version,
                    provider_response_id=revision_invocation.provider_response_id,
                    usage=revision_invocation.usage,
                    tool_audit=(),
                    reasoning_summary=revision_invocation.reasoning_summary,
                    completed_at=self._clock(),
                )
                return ConversationOutcome.SUCCEEDED
            request = ConversationAgentRequest(
                turn_id=claim.turn_id,
                conversation_id=claim.conversation_id,
                message=subject.turn.text,
                history=subject.history,
                context=context,
                is_email_situation=subject.situation_type is SituationType.EMAIL_THREAD,
            )
            invocation, preparation = await self._respond(subject, request)
            completed_at = self._clock()
            await self._conversations.complete(
                claim,
                response_text=self._normal_response_text(
                    invocation,
                    preparation,
                    revision_status,
                ),
                agent_version=self._agent_version,
                provider_response_id=invocation.provider_response_id,
                usage=invocation.usage,
                tool_audit=invocation.tool_audit,
                reasoning_summary=invocation.result.reasoning_summary,
                proposed_actions=invocation.result.proposed_actions,
                prepared_actions=preparation.proposals,
                memory_proposals=invocation.result.memory_proposals,
                completed_at=completed_at,
            )
            if self._memory_learner is not None:
                await self._memory_learner.learn(
                    invocation.result.memory_proposals,
                    user_id=claim.user_id,
                    workspace_id=claim.workspace_id,
                    situation_id=subject.conversation.situation_id,
                    goal_ids=tuple(goal.id for goal in context.goals),
                    source_type=MemorySourceType.AGENT_INFERRED,
                    source_ref=f"telegram-conversation:{claim.turn_id}",
                    occurred_at=completed_at,
                    episode_type=MemoryEpisodeType.EXPERIENCE,
                )
            return ConversationOutcome.SUCCEEDED
        except ConversationModelOutputError:
            return await self._retry(claim, "MODEL_OUTPUT_INVALID")
        except ConversationPermanentError, AgentPermanentError, AgentToolBudgetExceeded:
            return await self._complete_fallback(claim, "PERMANENT_CONVERSATION_FAILURE")
        except ConversationScopeError, MemoryNotFoundError:
            await self._record_failure(
                claim,
                retryable=False,
                code="INVALID_SCOPE",
                summary="The authenticated conversation scope is unavailable.",
            )
            return ConversationOutcome.TERMINAL
        except (
            ConversationTransientError,
            GmailProviderError,
            SecretManagerProviderError,
        ):
            return await self._retry(claim, "PROVIDER_UNAVAILABLE")
        except Exception:
            # Raw provider/model errors can contain private content and are never persisted.
            return await self._retry(claim, "UNEXPECTED_CONVERSATION_FAILURE")
        finally:
            if typing_task is not None:
                typing_task.cancel()
                with suppress(asyncio.CancelledError):
                    await typing_task

    async def _process_revision(
        self,
        claim: ConversationTurnClaim,
        subject: ConversationTurnSubject,
        context: AgentWorkingContext,
    ) -> tuple[RevisionLookupStatus, DraftRevisionInvocationResult | None]:
        revisions = self._action_revisions
        revision_agent = self._revision_agent
        if revisions is None or revision_agent is None:
            return RevisionLookupStatus.NONE, None
        lookup = await revisions.load(
            telegram_account_id=subject.telegram_account_id,
            user_id=claim.user_id,
            workspace_id=claim.workspace_id,
            now=self._clock(),
        )
        if lookup.status is not RevisionLookupStatus.ACTIVE:
            return lookup.status, None
        revision = lookup.context
        assert revision is not None
        if revision.conversation_id != claim.conversation_id:
            raise ConversationScopeError("revision conversation is unavailable")
        request = DraftRevisionRequest(
            turn_id=claim.turn_id,
            conversation_id=claim.conversation_id,
            instruction=subject.turn.text,
            current_message=revision.current_message,
            history=subject.history,
            context=context,
        )
        invocation = await revision_agent.revise(request)
        try:
            await revisions.complete(
                session_id=revision.session_id,
                current=revision.current_message,
                candidate=invocation.candidate,
                conversation_turn_id=claim.turn_id,
                now=self._clock(),
            )
        except ActionValidationError as error:
            raise ConversationModelOutputError("draft revision output is invalid") from error
        except ActionScopeError as error:
            raise ConversationScopeError("revision scope is unavailable") from error
        return lookup.status, invocation

    @staticmethod
    def _normal_response_text(
        invocation: ConversationInvocationResult,
        preparation: ActionProposalPreparation,
        revision_status: RevisionLookupStatus,
    ) -> str:
        response = preparation.clarification or invocation.result.message
        if revision_status is RevisionLookupStatus.EXPIRED:
            return (
                "That draft revision window expired, so I treated this as a normal message. "
                + response
            )
        return response

    async def _refresh_typing(self, chat_id: int) -> None:
        telegram = self._telegram
        if telegram is None:
            return
        while True:
            try:
                await telegram.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                # Typing is cosmetic and must never affect conversation delivery.
                pass
            await asyncio.sleep(self._typing_refresh_seconds)

    async def _respond(
        self, subject: ConversationTurnSubject, request: ConversationAgentRequest
    ) -> tuple[ConversationInvocationResult, ActionProposalPreparation]:
        if subject.connector_id is None or subject.secret_reference is None:
            invocation = await self._agent.respond(request, None)
            return invocation, await self._prepare_actions(invocation, None, subject)
        connector_id = subject.connector_id
        secret_reference = subject.secret_reference
        try:
            grant = await self._credential_store.get(secret_reference)
            gmail_client = await self._gmail_clients.create(grant)
        except AuthorizationRevoked, InvalidAuthorizedUserCredentials:
            invocation = await self._agent.respond(request, None)
            return invocation, await self._prepare_actions(invocation, None, subject)

        async def respond() -> tuple[ConversationInvocationResult, ActionProposalPreparation]:
            reader = ScopedGmailInvestigationReader(
                gmail_client,
                connector_id=connector_id,
                user_id=subject.turn.user_id,
                workspace_id=subject.turn.workspace_id,
                thread_id=subject.gmail_thread_id,
                thread_message_limit=self._thread_message_limit,
                search_result_limit=self._search_result_limit,
                body_max_chars=self._body_max_chars,
            )
            invocation = await self._agent.respond(request, reader)
            return invocation, await self._prepare_actions(invocation, reader, subject)

        return await use_gmail_client(gmail_client, respond)

    async def _prepare_actions(
        self,
        invocation: ConversationInvocationResult,
        reader: ScopedGmailInvestigationReader | None,
        subject: ConversationTurnSubject,
    ) -> ActionProposalPreparation:
        if self._action_proposals is None:
            return ActionProposalPreparation()
        return await self._action_proposals.prepare_model_proposals(
            invocation.result.proposed_actions,
            reader=reader,
            allowed_thread_id=subject.gmail_thread_id,
            account_identity=subject.connector_identity or "",
        )

    async def _retry(self, claim: ConversationTurnClaim, code: str) -> ConversationOutcome:
        if claim.attempt_count >= self._max_attempts:
            return await self._complete_fallback(claim, code)
        delay = min(
            self._retry_max_backoff_seconds,
            self._retry_initial_backoff_seconds * (2 ** max(0, claim.attempt_count - 1)),
        )
        await self._record_failure(
            claim,
            retryable=True,
            code=code,
            summary="The conversation turn failed transiently and may be retried.",
            next_retry_at=self._clock() + timedelta(seconds=delay),
        )
        return ConversationOutcome.RETRY

    async def _complete_fallback(
        self, claim: ConversationTurnClaim, code: str
    ) -> ConversationOutcome:
        # A model/tool failure is not a reason to leave the user with an unanswered message.
        await self._conversations.complete(
            claim,
            response_text=(
                "I’m sorry—I hit a problem while putting that answer together. "
                "Please try once more, and I’ll take another run at it."
            ),
            agent_version=self._agent_version,
            provider_response_id=None,
            usage=AgentUsage(),
            tool_audit=(),
            reasoning_summary=f"Safe conversation fallback: {code}.",
            completed_at=self._clock(),
        )
        return ConversationOutcome.SUCCEEDED

    async def _record_failure(
        self,
        claim: ConversationTurnClaim,
        *,
        retryable: bool,
        code: str,
        summary: str,
        next_retry_at: datetime | None = None,
    ) -> ConversationTurnRecord:
        return await self._conversations.fail(
            claim,
            retryable=retryable,
            max_attempts=self._max_attempts,
            next_retry_at=next_retry_at,
            failure_code=code,
            failure_summary=summary,
            completed_at=self._clock(),
        )
