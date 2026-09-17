import asyncio
import base64
import hashlib
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import Message
from email.parser import BytesParser
from typing import Protocol
from uuid import UUID

from eva_ai.actions.canonical import canonical_email_hash
from eva_ai.actions.errors import ActionValidationError
from eva_ai.actions.policy import ActionPolicyContext, ActionPolicyEngine
from eva_ai.actions.types import (
    ActionClaim,
    ActionClaimOutcome,
    ActionClaimResult,
    ActionExecutionOutcome,
    ActionExecutionResult,
    ActionExecutionSubject,
    ActionProposalStatus,
    ActionTaskRequest,
    ApprovalStatus,
    GmailActionCapability,
    PolicyDecision,
)
from eva_ai.connectors.gmail.actions import GmailDraftActionAdapter
from eva_ai.connectors.gmail.contracts import (
    AuthorizationRevoked,
    GmailActionClientFactory,
)
from eva_ai.connectors.gmail.mime import build_gmail_mime_message
from eva_ai.connectors.types import ConnectorStatus
from eva_ai.integrations.gmail.api import (
    GmailActionReauthorizationRequired,
    InvalidAuthorizedUserCredentials,
)
from eva_ai.integrations.gmail.oauth import GMAIL_COMPOSE_SCOPE


class ActionExecutorStore(Protocol):
    async def claim_action(
        self,
        request: ActionTaskRequest,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> ActionClaimResult: ...

    async def load_execution_subject(self, claim: ActionClaim) -> ActionExecutionSubject: ...

    async def mark_provider_call_started(
        self,
        claim: ActionClaim,
        *,
        started_at: datetime,
    ) -> bool: ...

    async def complete_create_draft(self, claim: ActionClaim, **values: object) -> object: ...

    async def complete_update_draft(self, claim: ActionClaim, **values: object) -> object: ...

    async def complete_delete_draft(self, claim: ActionClaim, **values: object) -> object: ...

    async def complete_send_draft(self, claim: ActionClaim, **values: object) -> object: ...

    async def release_pre_provider_failure(
        self,
        claim: ActionClaim,
        *,
        failed_at: datetime,
    ) -> None: ...

    async def fail_validation(self, claim: ActionClaim, *, failed_at: datetime) -> None: ...

    async def mark_execution_unknown(
        self,
        claim: ActionClaim,
        *,
        failed_at: datetime,
    ) -> None: ...

    async def mark_execution_unavailable(
        self,
        claim: ActionClaim,
        *,
        connector_id: UUID,
        read_access_preserved: bool,
        failed_at: datetime,
    ) -> None: ...


class ActionCredentialStore(Protocol):
    async def get(self, secret_reference: str) -> str: ...


class ActionExecutor:
    def __init__(
        self,
        repository: ActionExecutorStore,
        credentials: ActionCredentialStore,
        gmail_factory: GmailActionClientFactory,
        *,
        lease_seconds: int,
        approval_ttl: timedelta,
        clock: Callable[[], datetime] | None = None,
        policy: ActionPolicyEngine | None = None,
    ) -> None:
        self._repository = repository
        self._credentials = credentials
        self._gmail_factory = gmail_factory
        self._lease_seconds = lease_seconds
        self._approval_ttl = approval_ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self._policy = policy or ActionPolicyEngine()

    async def execute(self, request: ActionTaskRequest) -> ActionExecutionResult:
        claim_result = await self._repository.claim_action(
            request,
            now=self._clock(),
            lease_seconds=self._lease_seconds,
        )
        if claim_result.outcome is ActionClaimOutcome.UNKNOWN:
            return _result(request, ActionExecutionOutcome.UNKNOWN, "unknown")
        if claim_result.outcome is ActionClaimOutcome.TERMINAL:
            return _result(request, ActionExecutionOutcome.TERMINAL)
        if claim_result.outcome is ActionClaimOutcome.BUSY:
            return _result(request, ActionExecutionOutcome.RETRY, "busy")
        claim = claim_result.claim
        assert claim is not None

        try:
            subject = await self._repository.load_execution_subject(claim)
            self._revalidate(subject, now=self._clock())
        except _ActionAuthorizationUnavailable:
            await self._repository.mark_execution_unavailable(
                claim,
                connector_id=subject.connector_id,
                read_access_preserved=True,
                failed_at=self._clock(),
            )
            return _result(request, ActionExecutionOutcome.UNAVAILABLE, "authorization")
        except ActionValidationError:
            await self._repository.fail_validation(claim, failed_at=self._clock())
            return _result(request, ActionExecutionOutcome.TERMINAL, "validation")
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._repository.release_pre_provider_failure(claim, failed_at=self._clock())
            return _result(request, ActionExecutionOutcome.RETRY, "infrastructure")

        assert subject.secret_reference is not None
        try:
            authorized_user_json = await self._credentials.get(subject.secret_reference)
            client = await self._gmail_factory.create_action(authorized_user_json)
        except GmailActionReauthorizationRequired:
            await self._repository.mark_execution_unavailable(
                claim,
                connector_id=subject.connector_id,
                read_access_preserved=True,
                failed_at=self._clock(),
            )
            return _result(request, ActionExecutionOutcome.UNAVAILABLE, "authorization")
        except InvalidAuthorizedUserCredentials:
            await self._repository.mark_execution_unavailable(
                claim,
                connector_id=subject.connector_id,
                read_access_preserved=False,
                failed_at=self._clock(),
            )
            return _result(request, ActionExecutionOutcome.UNAVAILABLE, "authorization")
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._repository.release_pre_provider_failure(claim, failed_at=self._clock())
            return _result(request, ActionExecutionOutcome.RETRY, "infrastructure")

        adapter = GmailDraftActionAdapter(client, sender=subject.connector_identity)
        boundary_started = False
        try:
            if subject.claim.capability is GmailActionCapability.SEND_DRAFT:
                provider_draft = await adapter.get_draft(_managed_draft_id(subject))
                if not _provider_draft_matches(subject, provider_draft):
                    await self._repository.fail_validation(claim, failed_at=self._clock())
                    return _result(request, ActionExecutionOutcome.TERMINAL, "validation")

            boundary_started = await self._repository.mark_provider_call_started(
                claim,
                started_at=self._clock(),
            )
            if not boundary_started:
                return _result(request, ActionExecutionOutcome.TERMINAL)

            provider_result = await self._execute_provider(adapter, subject)
            await self._complete(claim, subject, provider_result)
            return _result(request, ActionExecutionOutcome.SUCCEEDED, "success")
        except GmailActionReauthorizationRequired:
            await self._repository.mark_execution_unavailable(
                claim,
                connector_id=subject.connector_id,
                read_access_preserved=True,
                failed_at=self._clock(),
            )
            return _result(request, ActionExecutionOutcome.UNAVAILABLE, "authorization")
        except AuthorizationRevoked:
            await self._repository.mark_execution_unavailable(
                claim,
                connector_id=subject.connector_id,
                read_access_preserved=False,
                failed_at=self._clock(),
            )
            return _result(request, ActionExecutionOutcome.UNAVAILABLE, "authorization")
        except ActionValidationError:
            await self._repository.fail_validation(claim, failed_at=self._clock())
            return _result(request, ActionExecutionOutcome.TERMINAL, "validation")
        except asyncio.CancelledError:
            if boundary_started:
                await self._repository.mark_execution_unknown(claim, failed_at=self._clock())
            raise
        except Exception:
            if boundary_started:
                await self._repository.mark_execution_unknown(claim, failed_at=self._clock())
                return _result(request, ActionExecutionOutcome.UNKNOWN, "unknown")
            await self._repository.release_pre_provider_failure(claim, failed_at=self._clock())
            return _result(request, ActionExecutionOutcome.RETRY, "provider")
        finally:
            try:
                await adapter.close()
            except BaseException:
                # The provider result and durable Action state take precedence over client cleanup.
                pass

    def _revalidate(self, subject: ActionExecutionSubject, *, now: datetime) -> None:
        claim = subject.claim
        proposal = subject.proposal
        if (
            claim.proposal_id != proposal.id
            or claim.user_id != proposal.user_id
            or claim.workspace_id != proposal.workspace_id
            or claim.capability != proposal.capability
            or proposal.connector_account_id != subject.connector_id
        ):
            raise ActionValidationError("action scope is invalid")
        if (
            subject.connector_status != ConnectorStatus.ACTIVE
            or GMAIL_COMPOSE_SCOPE not in subject.connector_scopes
            or not subject.secret_reference
        ):
            raise _ActionAuthorizationUnavailable
        if canonical_email_hash(proposal.message) != proposal.parameters_hash:
            raise ActionValidationError("action content hash is invalid")
        if proposal.expires_at is not None and now >= proposal.expires_at:
            raise ActionValidationError("action proposal has expired")

        managed = subject.managed_draft
        context = ActionPolicyContext(
            scoped_connector=True,
            managed_draft=managed is not None,
            authenticated_discard=(
                proposal.origin.value == "TELEGRAM_USER"
                and claim.capability is GmailActionCapability.DELETE_DRAFT
            ),
            exact_approval=_has_exact_approval(subject, now=now),
        )
        decision = self._policy.evaluate(claim.capability, context)
        expected_decision = (
            PolicyDecision.REQUIRE_APPROVAL
            if claim.capability is GmailActionCapability.SEND_DRAFT
            else PolicyDecision.ALLOW
        )
        if decision is not expected_decision or proposal.policy_decision is not expected_decision:
            raise ActionValidationError("action policy denied execution")
        if claim.capability is GmailActionCapability.SEND_DRAFT:
            if proposal.status is not ActionProposalStatus.APPROVED or not context.exact_approval:
                raise ActionValidationError("send approval is unavailable")
        elif proposal.status is not ActionProposalStatus.QUEUED:
            raise ActionValidationError("action proposal is not queued")
        if (
            claim.capability
            in {
                GmailActionCapability.CREATE_DRAFT,
                GmailActionCapability.UPDATE_DRAFT,
            }
            and subject.telegram_principal is None
        ):
            raise ActionValidationError("approval delivery principal is unavailable")
        if managed is not None and managed.connector_account_id != subject.connector_id:
            raise ActionValidationError("managed draft connector scope is invalid")

    async def _execute_provider(
        self,
        adapter: GmailDraftActionAdapter,
        subject: ActionExecutionSubject,
    ) -> object:
        match subject.claim.capability:
            case GmailActionCapability.CREATE_DRAFT:
                return await adapter.create_draft(
                    subject.proposal.message,
                    rfc_message_id=_new_rfc_message_id(subject.claim.action_id),
                )
            case GmailActionCapability.UPDATE_DRAFT:
                assert subject.managed_draft is not None
                return await adapter.update_draft(
                    subject.managed_draft.provider_draft_id,
                    subject.proposal.message,
                    rfc_message_id=subject.managed_draft.rfc_message_id,
                )
            case GmailActionCapability.DELETE_DRAFT:
                await adapter.delete_draft(_managed_draft_id(subject))
                return None
            case GmailActionCapability.SEND_DRAFT:
                return await adapter.send_draft(_managed_draft_id(subject))

    async def _complete(
        self,
        claim: ActionClaim,
        subject: ActionExecutionSubject,
        provider_result: object,
    ) -> None:
        from eva_ai.connectors.gmail.contracts import GmailDraftResult, GmailSendResult

        completed_at = self._clock()
        if claim.capability is GmailActionCapability.CREATE_DRAFT:
            assert isinstance(provider_result, GmailDraftResult)
            assert subject.telegram_principal is not None
            await self._repository.complete_create_draft(
                claim,
                provider_draft_id=provider_result.draft_id,
                provider_message_id=provider_result.message_id,
                provider_thread_id=provider_result.thread_id,
                rfc_message_id=_new_rfc_message_id(claim.action_id),
                telegram_account_id=subject.telegram_principal.telegram_account_id,
                provider_chat_id=subject.telegram_principal.provider_chat_id,
                callback_token_digest=_callback_token_digest(),
                approval_expires_at=completed_at + self._approval_ttl,
                completed_at=completed_at,
            )
        elif claim.capability is GmailActionCapability.UPDATE_DRAFT:
            assert isinstance(provider_result, GmailDraftResult)
            assert subject.telegram_principal is not None
            await self._repository.complete_update_draft(
                claim,
                provider_draft_id=provider_result.draft_id,
                provider_message_id=provider_result.message_id,
                provider_thread_id=provider_result.thread_id,
                telegram_account_id=subject.telegram_principal.telegram_account_id,
                provider_chat_id=subject.telegram_principal.provider_chat_id,
                callback_token_digest=_callback_token_digest(),
                approval_expires_at=completed_at + self._approval_ttl,
                completed_at=completed_at,
            )
        elif claim.capability is GmailActionCapability.DELETE_DRAFT:
            await self._repository.complete_delete_draft(
                claim,
                provider_draft_id=_managed_draft_id(subject),
                completed_at=completed_at,
            )
        else:
            assert isinstance(provider_result, GmailSendResult)
            await self._repository.complete_send_draft(
                claim,
                provider_draft_id=_managed_draft_id(subject),
                provider_message_id=provider_result.message_id,
                provider_thread_id=provider_result.thread_id,
                completed_at=completed_at,
            )


class _ActionAuthorizationUnavailable(RuntimeError):
    pass


def _has_exact_approval(subject: ActionExecutionSubject, *, now: datetime) -> bool:
    approval = subject.approval
    managed = subject.managed_draft
    proposal = subject.proposal
    return bool(
        approval is not None
        and managed is not None
        and approval.status is ApprovalStatus.GRANTED
        and approval.proposal_id == proposal.id
        and approval.user_id == proposal.user_id
        and approval.workspace_id == proposal.workspace_id
        and approval.parameters_hash == proposal.parameters_hash
        and managed.active_send_proposal_id == proposal.id
        and managed.current_content_hash == proposal.parameters_hash
        and now < approval.expires_at
    )


def _managed_draft_id(subject: ActionExecutionSubject) -> str:
    if subject.managed_draft is None:
        raise ActionValidationError("managed draft is unavailable")
    return subject.managed_draft.provider_draft_id


def _provider_draft_matches(
    subject: ActionExecutionSubject,
    provider_draft: Mapping[str, object],
) -> bool:
    managed = subject.managed_draft
    if managed is None or provider_draft.get("id") != managed.provider_draft_id:
        return False
    provider_message = provider_draft.get("message")
    if not isinstance(provider_message, Mapping):
        return False
    raw = provider_message.get("raw")
    if not isinstance(raw, str):
        return False
    expected = build_gmail_mime_message(
        subject.proposal.message,
        sender=subject.connector_identity,
        rfc_message_id=managed.rfc_message_id,
    )
    try:
        actual_bytes = base64.b64decode(
            raw + ("=" * (-len(raw) % 4)), altchars=b"-_", validate=True
        )
        expected_bytes = base64.b64decode(
            expected.raw + ("=" * (-len(expected.raw) % 4)), altchars=b"-_", validate=True
        )
        actual_projection = _mime_projection(
            BytesParser(policy=policy.default).parsebytes(actual_bytes),
            root=True,
        )
        expected_projection = _mime_projection(
            BytesParser(policy=policy.default).parsebytes(expected_bytes),
            root=True,
        )
    except ValueError, TypeError:
        return False
    return actual_projection is not None and actual_projection == expected_projection


_GMAIL_OWNED_DRAFT_HEADERS = frozenset({"date", "message-id", "received"})
_STRUCTURAL_MIME_HEADERS = frozenset({"content-disposition", "content-type"})


def _mime_projection(message: Message, *, root: bool) -> object | None:
    """Return send-relevant MIME content while excluding Gmail-owned transport metadata."""

    if message.defects:
        return None
    ignored_headers = _STRUCTURAL_MIME_HEADERS
    if root:
        ignored_headers = ignored_headers | _GMAIL_OWNED_DRAFT_HEADERS
    headers = tuple(
        sorted(
            (
                name.lower(),
                # Parsed header values remove harmless folding while preserving semantic content.
                " ".join(str(value).split()),
            )
            for name, value in message.items()
            if name.lower() not in ignored_headers
        )
    )
    content_type_parameters = _mime_parameters(
        message,
        header="content-type",
        excluded=frozenset({"boundary"}),
    )
    disposition_parameters = _mime_parameters(
        message,
        header="content-disposition",
        excluded=frozenset(),
    )
    if message.is_multipart():
        payload = message.get_payload()
        if not isinstance(payload, list):
            return None
        child_projections: list[object] = []
        for part in payload:
            if not isinstance(part, Message):
                return None
            child_projection = _mime_projection(part, root=False)
            if child_projection is None:
                return None
            child_projections.append(child_projection)
        body: object = tuple(child_projections)
    else:
        decoded = message.get_payload(decode=True)
        if not isinstance(decoded, bytes):
            return None
        # Gmail may normalize RFC line endings while retaining identical text content.
        body = (
            decoded.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            if message.get_content_maintype() == "text"
            else decoded
        )
    return (
        headers,
        message.get_content_type().lower(),
        content_type_parameters,
        message.get_content_disposition(),
        disposition_parameters,
        message.get_filename(),
        body,
    )


def _mime_parameters(
    message: Message,
    *,
    header: str,
    excluded: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    parameters = message.get_params(failobj=[], header=header) or []
    return tuple(
        sorted(
            (str(name).lower(), "" if value is None else str(value))
            for name, value in parameters[1:]
            if str(name).lower() not in excluded
        )
    )


def _new_rfc_message_id(action_id: object) -> str:
    return f"<{action_id}@eva.evaatyourservice.com>"


def _callback_token_digest() -> str:
    token = secrets.token_urlsafe(32)
    return hashlib.sha256(token.encode()).hexdigest()


def _result(
    request: ActionTaskRequest,
    outcome: ActionExecutionOutcome,
    category: str | None = None,
) -> ActionExecutionResult:
    return ActionExecutionResult(
        action_id=request.action_id,
        outcome=outcome,
        provider_status_category=category,
    )
