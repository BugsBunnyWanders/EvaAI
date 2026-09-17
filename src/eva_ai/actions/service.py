import hashlib
import re
from datetime import datetime, timedelta
from email.utils import getaddresses
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from eva_ai.actions.canonical import CanonicalEmail
from eva_ai.actions.contracts import ActionProposalSessionStore
from eva_ai.actions.errors import ActionValidationError
from eva_ai.actions.policy import ActionPolicyContext, ActionPolicyEngine
from eva_ai.actions.recipients import (
    RecipientCandidate,
    RecipientResolutionStatus,
    resolve_recipient,
)
from eva_ai.actions.types import (
    ActionOrigin,
    ActionProposalPreparation,
    AllowedActionCreation,
    ApprovalCallbackOutcome,
    ApprovalCallbackResult,
    ApprovalDecision,
    ApprovalRecord,
    DraftRevisionCandidate,
    GmailActionCapability,
    NewActionProposal,
    PolicyDecision,
    PreparedActionProposal,
    RevisionLookup,
)
from eva_ai.agent.contracts import GmailInvestigationReader
from eva_ai.agent.types import GmailMessageEvidence, ProposedAction

_RFC_MESSAGE_ID_PATTERN = re.compile(r"^<[^<>\s]+>$")
_APPROVAL_CALLBACK_PATTERN = re.compile(r"^(send|change|discard):([A-Za-z0-9_-]{16,48})$")


class ActionRevisionStore(Protocol):
    async def load_revision(
        self,
        *,
        telegram_account_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        now: datetime,
    ) -> RevisionLookup: ...

    async def complete_revision(
        self,
        *,
        session_id: UUID,
        replacement: CanonicalEmail,
        conversation_turn_id: UUID | None,
        destination: str,
        now: datetime,
    ) -> object: ...


class ActionRevisionService:
    def __init__(self, store: ActionRevisionStore, *, destination: str) -> None:
        self._store = store
        self._destination = destination

    async def load(
        self,
        *,
        telegram_account_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        now: datetime,
    ) -> RevisionLookup:
        return await self._store.load_revision(
            telegram_account_id=telegram_account_id,
            user_id=user_id,
            workspace_id=workspace_id,
            now=now,
        )

    @staticmethod
    def validate_replacement(
        current: CanonicalEmail,
        candidate: DraftRevisionCandidate,
    ) -> CanonicalEmail:
        if candidate.mode != current.mode:
            raise ActionValidationError("revision cannot change the email mode")
        if candidate.thread_id != current.thread_id:
            raise ActionValidationError("revision cannot change the Gmail thread")
        if candidate.attachments:
            raise ActionValidationError("revision attachments are not supported")
        try:
            return CanonicalEmail(
                mode=current.mode,
                to=candidate.to,
                cc=candidate.cc,
                bcc=candidate.bcc,
                subject=candidate.subject,
                text_body=candidate.text_body,
                html_body=candidate.html_body,
                thread_id=current.thread_id,
                # These provider-owned fields never come from model output.
                in_reply_to=current.in_reply_to,
                references=current.references,
            )
        except ValidationError as error:
            raise ActionValidationError("revision produced an invalid email") from error

    async def complete(
        self,
        *,
        session_id: UUID,
        current: CanonicalEmail,
        candidate: DraftRevisionCandidate,
        conversation_turn_id: UUID | None = None,
        now: datetime,
    ) -> object:
        replacement = self.validate_replacement(current, candidate)
        return await self._store.complete_revision(
            session_id=session_id,
            replacement=replacement,
            conversation_turn_id=conversation_turn_id,
            destination=self._destination,
            now=now,
        )


class ActionApprovalStore(Protocol):
    async def grant_and_queue_send(
        self,
        *,
        callback_token_digest: str,
        telegram_account_id: UUID,
        chat_id: int,
        destination: str,
        now: datetime,
    ) -> ApprovalDecision: ...

    async def discard_and_queue_delete(
        self,
        *,
        callback_token_digest: str,
        telegram_account_id: UUID,
        chat_id: int,
        destination: str,
        now: datetime,
    ) -> ApprovalDecision: ...

    async def open_revision(
        self,
        *,
        callback_token_digest: str,
        telegram_account_id: UUID,
        chat_id: int,
        revision_ttl: timedelta,
        now: datetime,
    ) -> ApprovalRecord: ...


class ActionApprovalService:
    def __init__(
        self,
        store: ActionApprovalStore,
        *,
        destination: str,
        revision_ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        self._store = store
        self._destination = destination
        self._revision_ttl = revision_ttl

    async def process_callback(
        self,
        callback_data: str,
        *,
        telegram_account_id: UUID,
        chat_id: int,
        now: datetime,
    ) -> ApprovalCallbackResult:
        match = _APPROVAL_CALLBACK_PATTERN.fullmatch(callback_data)
        if match is None:
            raise ActionValidationError("approval callback is invalid")
        operation, token = match.groups()
        digest = hashlib.sha256(token.encode()).hexdigest()
        if operation == "change":
            await self._store.open_revision(
                callback_token_digest=digest,
                telegram_account_id=telegram_account_id,
                chat_id=chat_id,
                revision_ttl=self._revision_ttl,
                now=now,
            )
            return ApprovalCallbackResult(outcome=ApprovalCallbackOutcome.CHANGE_REQUESTED)
        if operation == "send":
            decision = await self._store.grant_and_queue_send(
                callback_token_digest=digest,
                telegram_account_id=telegram_account_id,
                chat_id=chat_id,
                destination=self._destination,
                now=now,
            )
        else:
            decision = await self._store.discard_and_queue_delete(
                callback_token_digest=digest,
                telegram_account_id=telegram_account_id,
                chat_id=chat_id,
                destination=self._destination,
                now=now,
            )
        return ApprovalCallbackResult(
            outcome=ApprovalCallbackOutcome(decision.outcome.value),
            action_id=None if decision.action is None else decision.action.id,
        )


class _DraftArguments(BaseModel):
    # Scope/provenance fields supplied by a model are intentionally ignored here. Repositories
    # provide those values from authenticated rows when the proposal is persisted.
    model_config = ConfigDict(frozen=True, extra="ignore")

    mode: Literal["NEW", "REPLY"]
    to: tuple[str, ...] = Field(max_length=50)
    cc: tuple[str, ...] = Field(default=(), max_length=50)
    bcc: tuple[str, ...] = Field(default=(), max_length=50)
    subject: str = Field(max_length=998)
    text_body: str = Field(max_length=100_000)
    html_body: str | None = Field(default=None, max_length=200_000)
    thread_id: str | None = Field(default=None, max_length=500)
    attachments: tuple[str, ...] = Field(default=(), max_length=1)

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def normalize_recipient_references(cls, value: object) -> object:
        if not isinstance(value, list | tuple):
            return value
        normalized: list[object] = []
        for item in value:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            cleaned = " ".join(item.split())
            if not cleaned:
                raise ValueError("recipient reference must not be blank")
            normalized.append(cleaned)
        return tuple(normalized)


class ActionProposalService:
    def __init__(
        self,
        store: ActionProposalSessionStore,
        *,
        destination: str,
        policy: ActionPolicyEngine | None = None,
    ) -> None:
        self._store = store
        self._destination = destination
        self._policy = policy or ActionPolicyEngine()

    async def prepare_model_proposals(
        self,
        proposals: tuple[ProposedAction, ...],
        *,
        reader: GmailInvestigationReader | None,
        allowed_thread_id: str | None,
        account_identity: str,
    ) -> ActionProposalPreparation:
        prepared: list[PreparedActionProposal] = []
        for model_proposal in proposals:
            if model_proposal.capability != GmailActionCapability.CREATE_DRAFT:
                continue
            try:
                arguments = _DraftArguments.model_validate(model_proposal.arguments)
            except ValidationError:
                continue
            if arguments.attachments:
                continue

            if not account_identity.strip():
                return ActionProposalPreparation(
                    clarification="Connect Gmail before I can create that draft."
                )

            # Validate reply scope before performing any broader Gmail history search. This
            # prevents a model-supplied cross-thread reply from expanding provider access.
            reply_context = await self._reply_context(
                arguments,
                reader=reader,
                allowed_thread_id=allowed_thread_id,
            )
            if arguments.mode == "REPLY" and reply_context is None:
                continue

            resolution = await self._resolve_addresses(
                arguments,
                reader=reader,
                account_identity=account_identity,
            )
            if isinstance(resolution, str):
                # Do not partially execute a batch while the user is clarifying its recipients.
                return ActionProposalPreparation(clarification=resolution)
            to, cc, bcc = resolution

            subject = arguments.subject
            thread_id: str | None = None
            in_reply_to: str | None = None
            references: tuple[str, ...] = ()
            if reply_context is not None:
                subject, thread_id, in_reply_to, references = reply_context
            try:
                message = CanonicalEmail(
                    mode=arguments.mode,
                    to=to,
                    cc=cc,
                    bcc=bcc,
                    subject=subject,
                    text_body=arguments.text_body,
                    html_body=arguments.html_body,
                    thread_id=thread_id,
                    in_reply_to=in_reply_to,
                    references=references,
                    attachments=arguments.attachments,
                )
            except ValidationError:
                continue
            prepared.append(
                PreparedActionProposal(
                    capability=GmailActionCapability.CREATE_DRAFT,
                    description=model_proposal.description,
                    message=message,
                )
            )
        return ActionProposalPreparation(proposals=tuple(prepared))

    async def create_from_model_proposals_in_session(
        self,
        session: AsyncSession,
        *,
        proposals: tuple[PreparedActionProposal, ...],
        user_id: UUID,
        workspace_id: UUID,
        connector_account_id: UUID,
        event_id: UUID,
        situation_id: UUID | None,
        agent_run_id: UUID | None,
        conversation_turn_id: UUID | None,
        origin: ActionOrigin,
        source_prefix: str,
        created_at: datetime,
    ) -> tuple[AllowedActionCreation, ...]:
        created: list[AllowedActionCreation] = []
        for index, prepared in enumerate(proposals):
            decision = self._policy.evaluate(
                prepared.capability,
                ActionPolicyContext(scoped_connector=True),
            )
            if decision is not PolicyDecision.ALLOW:
                continue
            proposal = NewActionProposal(
                user_id=user_id,
                workspace_id=workspace_id,
                connector_account_id=connector_account_id,
                event_id=event_id,
                situation_id=situation_id,
                agent_run_id=agent_run_id,
                conversation_turn_id=conversation_turn_id,
                origin=origin,
                capability=prepared.capability,
                message=prepared.message,
                description=prepared.description,
                policy_decision=decision,
                source_key=f"{source_prefix}:action:{index}",
                proposal_family_key=f"{source_prefix}:action:{index}",
                created_at=created_at,
            )
            created.append(
                await self._store.create_allowed_action_in_session(
                    session,
                    proposal=proposal,
                    destination=self._destination,
                )
            )
        return tuple(created)

    async def _resolve_addresses(
        self,
        arguments: _DraftArguments,
        *,
        reader: GmailInvestigationReader | None,
        account_identity: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | str:
        groups: list[tuple[str, ...]] = []
        for values in (arguments.to, arguments.cc, arguments.bcc):
            resolved: list[str] = []
            for value in values:
                initial = resolve_recipient(value, ())
                if initial.status is RecipientResolutionStatus.RESOLVED:
                    assert initial.email_address is not None
                    resolved.append(initial.email_address)
                    continue
                if reader is None:
                    assert initial.clarification is not None
                    return initial.clarification
                evidence = await reader.search(_recipient_query(value))
                candidates = _recipient_candidates(evidence.messages, account_identity)
                result = resolve_recipient(value, candidates)
                if result.status is RecipientResolutionStatus.CLARIFICATION_REQUIRED:
                    assert result.clarification is not None
                    return result.clarification
                assert result.email_address is not None
                resolved.append(result.email_address)
            groups.append(tuple(resolved))
        return groups[0], groups[1], groups[2]

    async def _reply_context(
        self,
        arguments: _DraftArguments,
        *,
        reader: GmailInvestigationReader | None,
        allowed_thread_id: str | None,
    ) -> tuple[str, str, str, tuple[str, ...]] | None:
        if arguments.mode == "NEW":
            return None
        if (
            reader is None
            or allowed_thread_id is None
            or (arguments.thread_id is not None and arguments.thread_id != allowed_thread_id)
        ):
            return None
        thread = await reader.read_thread()
        if thread.thread_id != allowed_thread_id or not thread.messages:
            return None
        references = tuple(
            message.rfc_message_id
            for message in thread.messages
            if _RFC_MESSAGE_ID_PATTERN.fullmatch(message.rfc_message_id) is not None
        )
        if not references:
            return None
        subject = _reply_subject(thread.messages[-1].subject)
        if subject is None:
            return None
        return subject, allowed_thread_id, references[-1], references


def _recipient_candidates(
    messages: tuple[GmailMessageEvidence, ...],
    account_identity: str,
) -> tuple[RecipientCandidate, ...]:
    owner = account_identity.strip().casefold()
    candidates: dict[str, RecipientCandidate] = {}
    for message in messages:
        for display_name, address in getaddresses((message.sender, message.recipients)):
            if not address or address.strip().casefold() == owner:
                continue
            try:
                candidate = RecipientCandidate(
                    display_name=display_name,
                    email_address=address,
                )
            except ValidationError:
                continue
            candidates.setdefault(candidate.email_address, candidate)
    return tuple(candidates.values())


def _recipient_query(value: str) -> str:
    normalized = " ".join(value.split())[:200]
    escaped = normalized.replace("\\", "\\\\").replace('"', '\\"')
    return f'from:"{escaped}" OR to:"{escaped}"'


def _reply_subject(subject: str) -> str | None:
    normalized = " ".join(subject.split())
    if not normalized:
        return None
    if normalized.casefold().startswith("re:"):
        return normalized
    return f"Re: {normalized}"
