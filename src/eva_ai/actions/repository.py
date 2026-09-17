from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid5, uuid7

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from eva_ai.actions.canonical import CanonicalEmail
from eva_ai.actions.errors import ActionConflictError, ActionScopeError, ActionValidationError
from eva_ai.actions.types import (
    ActionClaim,
    ActionClaimOutcome,
    ActionClaimResult,
    ActionExecutionRequestedMessage,
    ActionExecutionSubject,
    ActionProposalRecord,
    ActionProposalStatus,
    ActionRecord,
    ActionStatus,
    ActionTaskRequest,
    AllowedActionCreation,
    ApprovalDecision,
    ApprovalDecisionOutcome,
    ApprovalRecord,
    ApprovalStatus,
    CreateDraftCompletion,
    GmailActionCapability,
    ManagedDraftStatus,
    ManagedGmailDraftRecord,
    NewActionProposal,
    PolicyDecision,
    TelegramApprovalPrincipal,
)
from eva_ai.connectors.types import ConnectorStatus
from eva_ai.db.models import (
    Action,
    ActionApproval,
    ActionProposal,
    ActionResult,
    ConnectorAccount,
    ManagedGmailDraft,
    OutboxMessage,
    TelegramAccount,
)
from eva_ai.db.session import Database
from eva_ai.events.types import OutboxState
from eva_ai.telegram.types import TelegramAccountStatus

_PROPOSAL_NAMESPACE = UUID("bf60a20d-3227-55b9-b4d0-ff7f60c63fe7")
_ACTION_NAMESPACE = UUID("764d2480-cac1-54f4-bc39-df6aac87faef")
_OUTBOX_NAMESPACE = UUID("f6161c88-4f27-52c6-a4bd-91d49bb6d786")
_DRAFT_NAMESPACE = UUID("98f8db31-dc4b-595f-b0c6-067d14438502")
_APPROVAL_NAMESPACE = UUID("f1af2fa2-1528-531b-a7f8-2f3e65f63657")


class ActionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create_allowed_action(
        self,
        *,
        proposal: NewActionProposal,
        destination: str,
    ) -> AllowedActionCreation:
        try:
            async with self._database.session() as session:
                async with session.begin():
                    return await self.create_allowed_action_in_session(
                        session,
                        proposal=proposal,
                        destination=destination,
                    )
        except IntegrityError:
            # Database constraints are the final scope boundary. Do not leak the violated resource
            # or provider-controlled values through the public repository error.
            raise ActionScopeError("action proposal scope is invalid") from None

    async def create_allowed_action_in_session(
        self,
        session: AsyncSession,
        *,
        proposal: NewActionProposal,
        destination: str,
    ) -> AllowedActionCreation:
        if proposal.policy_decision is not PolicyDecision.ALLOW:
            raise ActionValidationError("only an allowed proposal can be queued without approval")
        if proposal.capability is GmailActionCapability.SEND_DRAFT:
            raise ActionValidationError("sending always requires an exact approval")

        proposal_id = uuid5(
            _PROPOSAL_NAMESPACE,
            f"{proposal.workspace_id}:{proposal.user_id}:{proposal.source_key}",
        )
        proposal_statement = (
            insert(ActionProposal)
            .values(
                id=proposal_id,
                user_id=proposal.user_id,
                workspace_id=proposal.workspace_id,
                connector_account_id=proposal.connector_account_id,
                event_id=proposal.event_id,
                situation_id=proposal.situation_id,
                goal_id=proposal.goal_id,
                agent_run_id=proposal.agent_run_id,
                conversation_turn_id=proposal.conversation_turn_id,
                supersedes_proposal_id=proposal.supersedes_proposal_id,
                source_key=proposal.source_key,
                proposal_family_key=proposal.proposal_family_key,
                origin=proposal.origin,
                capability=proposal.capability,
                parameters_json=proposal.message.model_dump(mode="json"),
                parameters_hash=proposal.parameters_hash,
                description=proposal.description,
                risk_level=proposal.risk_level,
                policy_decision=proposal.policy_decision,
                status=ActionProposalStatus.QUEUED,
                version=proposal.version,
                created_at=proposal.created_at,
                expires_at=proposal.expires_at,
            )
            .on_conflict_do_nothing(constraint="uq_action_proposals_scope_source_key")
            .returning(ActionProposal)
        )
        proposal_row = (await session.scalars(proposal_statement)).one_or_none()
        if proposal_row is None:
            proposal_row = await session.scalar(
                select(ActionProposal).where(
                    ActionProposal.workspace_id == proposal.workspace_id,
                    ActionProposal.user_id == proposal.user_id,
                    ActionProposal.source_key == proposal.source_key,
                )
            )
            if proposal_row is None:
                raise ActionConflictError("action proposal was not visible after deduplication")
            action_row = await session.scalar(
                select(Action).where(
                    Action.proposal_id == proposal_row.id,
                    Action.workspace_id == proposal.workspace_id,
                    Action.user_id == proposal.user_id,
                )
            )
            if action_row is None:
                raise ActionConflictError("deduplicated action was not visible")
            return AllowedActionCreation(
                proposal=_proposal_record(proposal_row),
                action=_action_record(action_row),
            )

        action_row = await self._queue_action_in_session(
            session,
            proposal=proposal_row,
            destination=destination,
            queued_at=proposal.created_at,
        )
        await session.flush()
        return AllowedActionCreation(
            proposal=_proposal_record(proposal_row),
            action=_action_record(action_row),
        )

    async def claim_action(
        self,
        request: ActionTaskRequest,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> ActionClaimResult:
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(Action).where(Action.id == request.action_id).with_for_update()
                )
                if row is None:
                    return ActionClaimResult(outcome=ActionClaimOutcome.TERMINAL)
                if row.status in {
                    ActionStatus.SUCCEEDED,
                    ActionStatus.FAILED,
                    ActionStatus.UNKNOWN,
                }:
                    return ActionClaimResult(outcome=ActionClaimOutcome.TERMINAL)
                if row.status == ActionStatus.RUNNING:
                    if row.lease_expires_at is not None and row.lease_expires_at > now:
                        return ActionClaimResult(outcome=ActionClaimOutcome.BUSY)
                    if row.provider_call_started_at is not None:
                        # Once a mutation may have reached Gmail, a lease timeout is uncertainty,
                        # not permission to repeat a potentially non-idempotent operation.
                        row.status = ActionStatus.UNKNOWN
                        row.finished_at = now
                        row.claim_id = None
                        row.lease_expires_at = None
                        row.failure_code = "provider_outcome_unknown"
                        row.failure_summary = "provider outcome requires reconciliation"
                        session.add(
                            ActionResult(
                                action_id=row.id,
                                user_id=row.user_id,
                                workspace_id=row.workspace_id,
                                outcome=ActionStatus.UNKNOWN,
                                provider_status_category="unknown",
                                result_metadata={},
                                created_at=now,
                            )
                        )
                        await session.flush()
                        return ActionClaimResult(outcome=ActionClaimOutcome.UNKNOWN)

                claim_id = uuid7()
                row.status = ActionStatus.RUNNING
                row.claim_id = claim_id
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                row.attempt_count += 1
                row.started_at = now
                row.finished_at = None
                row.failure_code = None
                row.failure_summary = None
                await session.flush()
                return ActionClaimResult(
                    outcome=ActionClaimOutcome.CLAIMED,
                    claim=ActionClaim(
                        action_id=row.id,
                        proposal_id=row.proposal_id,
                        claim_id=claim_id,
                        user_id=row.user_id,
                        workspace_id=row.workspace_id,
                        capability=row.capability,
                        attempt_count=row.attempt_count,
                    ),
                )

    async def mark_provider_call_started(
        self,
        claim: ActionClaim,
        *,
        started_at: datetime,
    ) -> bool:
        statement = (
            update(Action)
            .where(_claim_predicate(claim), Action.provider_call_started_at.is_(None))
            .values(provider_call_started_at=started_at)
            .returning(Action.id)
        )
        async with self._database.session() as session:
            async with session.begin():
                return (await session.scalar(statement)) is not None

    async def load_execution_subject(self, claim: ActionClaim) -> ActionExecutionSubject:
        async with self._database.session() as session:
            values = (
                await session.execute(
                    select(Action, ActionProposal, ConnectorAccount)
                    .join(
                        ActionProposal,
                        and_(
                            ActionProposal.id == Action.proposal_id,
                            ActionProposal.workspace_id == Action.workspace_id,
                            ActionProposal.user_id == Action.user_id,
                        ),
                    )
                    .join(
                        ConnectorAccount,
                        and_(
                            ConnectorAccount.id == ActionProposal.connector_account_id,
                            ConnectorAccount.workspace_id == Action.workspace_id,
                            ConnectorAccount.user_id == Action.user_id,
                        ),
                    )
                    .where(_claim_predicate(claim))
                )
            ).one_or_none()
            if values is None:
                raise ActionConflictError("action execution subject is unavailable")
            _, proposal, connector = values

            managed_draft: ManagedGmailDraft | None = None
            if claim.capability == GmailActionCapability.SEND_DRAFT:
                managed_draft = await session.scalar(
                    select(ManagedGmailDraft).where(
                        ManagedGmailDraft.active_send_proposal_id == proposal.id,
                        ManagedGmailDraft.workspace_id == claim.workspace_id,
                        ManagedGmailDraft.user_id == claim.user_id,
                    )
                )
            elif (
                claim.capability
                in {
                    GmailActionCapability.UPDATE_DRAFT,
                    GmailActionCapability.DELETE_DRAFT,
                }
                and proposal.supersedes_proposal_id is not None
            ):
                managed_draft = await session.scalar(
                    select(ManagedGmailDraft).where(
                        ManagedGmailDraft.active_send_proposal_id
                        == proposal.supersedes_proposal_id,
                        ManagedGmailDraft.workspace_id == claim.workspace_id,
                        ManagedGmailDraft.user_id == claim.user_id,
                    )
                )

            approval: ActionApproval | None = None
            if claim.capability == GmailActionCapability.SEND_DRAFT:
                approval = await session.scalar(
                    select(ActionApproval).where(
                        ActionApproval.proposal_id == proposal.id,
                        ActionApproval.workspace_id == claim.workspace_id,
                        ActionApproval.user_id == claim.user_id,
                    )
                )
            telegram = await session.scalar(
                select(TelegramAccount)
                .where(
                    TelegramAccount.workspace_id == claim.workspace_id,
                    TelegramAccount.user_id == claim.user_id,
                    TelegramAccount.status == TelegramAccountStatus.ACTIVE,
                )
                .limit(1)
            )

        return ActionExecutionSubject(
            claim=claim,
            proposal=_proposal_record(proposal),
            connector_id=connector.id,
            connector_identity=connector.account_identity,
            connector_status=connector.status,
            connector_scopes=tuple(connector.granted_scopes),
            secret_reference=connector.secret_reference,
            managed_draft=None if managed_draft is None else _managed_draft_record(managed_draft),
            approval=None if approval is None else _approval_record(approval),
            telegram_principal=(
                None
                if telegram is None
                else TelegramApprovalPrincipal(
                    telegram_account_id=telegram.id,
                    provider_chat_id=telegram.chat_id,
                )
            ),
        )

    async def release_pre_provider_failure(
        self,
        claim: ActionClaim,
        *,
        failed_at: datetime,
    ) -> None:
        del failed_at
        statement = (
            update(Action)
            .where(_claim_predicate(claim), Action.provider_call_started_at.is_(None))
            .values(
                status=ActionStatus.QUEUED,
                claim_id=None,
                lease_expires_at=None,
                failure_code="pre_provider_failure",
                failure_summary="execution will be retried",
            )
        )
        async with self._database.session() as session:
            async with session.begin():
                await session.execute(statement)

    async def fail_validation(self, claim: ActionClaim, *, failed_at: datetime) -> None:
        await self._finish_failed(
            claim,
            failed_at=failed_at,
            action_status=ActionStatus.FAILED,
            failure_code="validation_failed",
            failure_summary="action validation failed",
            provider_status_category="validation",
        )

    async def mark_execution_unknown(self, claim: ActionClaim, *, failed_at: datetime) -> None:
        await self._finish_failed(
            claim,
            failed_at=failed_at,
            action_status=ActionStatus.UNKNOWN,
            failure_code="provider_outcome_unknown",
            failure_summary="provider outcome requires reconciliation",
            provider_status_category="unknown",
        )

    async def mark_execution_unavailable(
        self,
        claim: ActionClaim,
        *,
        connector_id: UUID,
        failed_at: datetime,
    ) -> None:
        async with self._database.session() as session:
            async with session.begin():
                action = await session.scalar(
                    select(Action).where(_claim_predicate(claim)).with_for_update()
                )
                if action is None:
                    return
                proposal = await session.scalar(
                    select(ActionProposal).where(
                        ActionProposal.id == claim.proposal_id,
                        ActionProposal.workspace_id == claim.workspace_id,
                        ActionProposal.user_id == claim.user_id,
                    )
                )
                connector = await session.scalar(
                    select(ConnectorAccount).where(
                        ConnectorAccount.id == connector_id,
                        ConnectorAccount.workspace_id == claim.workspace_id,
                        ConnectorAccount.user_id == claim.user_id,
                    )
                )
                action.status = ActionStatus.FAILED
                action.finished_at = failed_at
                action.claim_id = None
                action.lease_expires_at = None
                action.failure_code = "action_reauthorization_required"
                action.failure_summary = "Gmail action authorization is unavailable"
                if proposal is not None:
                    proposal.status = ActionProposalStatus.FAILED
                    proposal.terminal_at = failed_at
                if connector is not None:
                    # Preserve the secret reference and Gmail sync cursor so read ingestion can be
                    # resumed or continue after the user expands the OAuth grant.
                    connector.status = ConnectorStatus.REAUTHORIZATION_REQUIRED
                    connector.last_error_type = "ActionAuthorizationUnavailable"
                    connector.last_error_summary = "operation failed"
                session.add(
                    ActionResult(
                        action_id=claim.action_id,
                        user_id=claim.user_id,
                        workspace_id=claim.workspace_id,
                        outcome=ActionStatus.FAILED,
                        provider_status_category="authorization",
                        result_metadata={},
                        created_at=failed_at,
                    )
                )

    async def _finish_failed(
        self,
        claim: ActionClaim,
        *,
        failed_at: datetime,
        action_status: ActionStatus,
        failure_code: str,
        failure_summary: str,
        provider_status_category: str,
    ) -> None:
        async with self._database.session() as session:
            async with session.begin():
                action = await session.scalar(
                    select(Action).where(_claim_predicate(claim)).with_for_update()
                )
                if action is None:
                    return
                proposal = await session.scalar(
                    select(ActionProposal).where(
                        ActionProposal.id == claim.proposal_id,
                        ActionProposal.workspace_id == claim.workspace_id,
                        ActionProposal.user_id == claim.user_id,
                    )
                )
                action.status = action_status
                action.finished_at = failed_at
                action.claim_id = None
                action.lease_expires_at = None
                action.failure_code = failure_code
                action.failure_summary = failure_summary
                if proposal is not None:
                    proposal.status = ActionProposalStatus.FAILED
                    proposal.terminal_at = failed_at
                managed = await session.scalar(
                    select(ManagedGmailDraft).where(
                        ManagedGmailDraft.active_send_proposal_id == claim.proposal_id,
                        ManagedGmailDraft.workspace_id == claim.workspace_id,
                        ManagedGmailDraft.user_id == claim.user_id,
                    )
                )
                if managed is not None and action_status == ActionStatus.UNKNOWN:
                    managed.status = ManagedDraftStatus.UNKNOWN
                    managed.updated_at = failed_at
                session.add(
                    ActionResult(
                        action_id=claim.action_id,
                        user_id=claim.user_id,
                        workspace_id=claim.workspace_id,
                        outcome=action_status,
                        provider_status_category=provider_status_category,
                        result_metadata={},
                        created_at=failed_at,
                    )
                )

    async def complete_create_draft(
        self,
        claim: ActionClaim,
        *,
        provider_draft_id: str,
        provider_message_id: str | None,
        provider_thread_id: str | None,
        rfc_message_id: str,
        telegram_account_id: UUID,
        provider_chat_id: int,
        callback_token_digest: str,
        approval_expires_at: datetime,
        completed_at: datetime,
    ) -> CreateDraftCompletion:
        async with self._database.session() as session:
            async with session.begin():
                values = (
                    await session.execute(
                        select(Action, ActionProposal)
                        .join(
                            ActionProposal,
                            and_(
                                ActionProposal.id == Action.proposal_id,
                                ActionProposal.workspace_id == Action.workspace_id,
                                ActionProposal.user_id == Action.user_id,
                            ),
                        )
                        .where(_claim_predicate(claim))
                        .with_for_update()
                    )
                ).one_or_none()
                if values is None:
                    raise ActionConflictError("action claim is stale")
                action_row, create_proposal = values
                if action_row.capability != GmailActionCapability.CREATE_DRAFT:
                    raise ActionValidationError("action is not a Gmail draft creation")
                message = CanonicalEmail.model_validate(create_proposal.parameters_json)

                action_row.status = ActionStatus.SUCCEEDED
                action_row.finished_at = completed_at
                action_row.claim_id = None
                action_row.lease_expires_at = None
                create_proposal.status = ActionProposalStatus.COMPLETED
                create_proposal.terminal_at = completed_at
                session.add(
                    ActionResult(
                        action_id=action_row.id,
                        user_id=action_row.user_id,
                        workspace_id=action_row.workspace_id,
                        outcome=ActionStatus.SUCCEEDED,
                        provider_status_category="success",
                        provider_draft_id=provider_draft_id,
                        provider_message_id=provider_message_id,
                        provider_thread_id=provider_thread_id,
                        result_metadata={},
                        created_at=completed_at,
                    )
                )

                draft_id = uuid5(_DRAFT_NAMESPACE, str(action_row.id))
                send_version = 1
                send_source_key = f"managed-draft:{draft_id}:send:{send_version}"
                send_proposal_id = uuid5(
                    _PROPOSAL_NAMESPACE,
                    f"{action_row.workspace_id}:{action_row.user_id}:{send_source_key}",
                )
                send_proposal = ActionProposal(
                    id=send_proposal_id,
                    user_id=action_row.user_id,
                    workspace_id=action_row.workspace_id,
                    connector_account_id=create_proposal.connector_account_id,
                    event_id=create_proposal.event_id,
                    situation_id=create_proposal.situation_id,
                    goal_id=create_proposal.goal_id,
                    agent_run_id=create_proposal.agent_run_id,
                    conversation_turn_id=create_proposal.conversation_turn_id,
                    supersedes_proposal_id=None,
                    source_key=send_source_key,
                    proposal_family_key=f"managed-draft:{draft_id}:send",
                    origin=create_proposal.origin,
                    capability=GmailActionCapability.SEND_DRAFT,
                    parameters_json=message.model_dump(mode="json"),
                    parameters_hash=create_proposal.parameters_hash,
                    description="Send the exact managed Gmail draft",
                    risk_level="high",
                    policy_decision=PolicyDecision.REQUIRE_APPROVAL,
                    status=ActionProposalStatus.WAITING_APPROVAL,
                    version=send_version,
                    created_at=completed_at,
                    expires_at=approval_expires_at,
                )
                approval = ActionApproval(
                    id=uuid5(_APPROVAL_NAMESPACE, str(send_proposal_id)),
                    proposal_id=send_proposal_id,
                    user_id=action_row.user_id,
                    workspace_id=action_row.workspace_id,
                    parameters_hash=create_proposal.parameters_hash,
                    principal_type="TELEGRAM",
                    telegram_account_id=telegram_account_id,
                    provider_chat_id=provider_chat_id,
                    status=ApprovalStatus.PENDING,
                    callback_token_digest=callback_token_digest,
                    requested_at=completed_at,
                    expires_at=approval_expires_at,
                )
                managed_draft = ManagedGmailDraft(
                    id=draft_id,
                    user_id=action_row.user_id,
                    workspace_id=action_row.workspace_id,
                    connector_account_id=create_proposal.connector_account_id,
                    provider_draft_id=provider_draft_id,
                    provider_message_id=provider_message_id,
                    gmail_thread_id=provider_thread_id,
                    create_proposal_id=create_proposal.id,
                    active_send_proposal_id=send_proposal_id,
                    send_proposal_version=send_version,
                    current_content_hash=create_proposal.parameters_hash,
                    rfc_message_id=rfc_message_id,
                    status=ManagedDraftStatus.READY,
                    created_at=completed_at,
                    updated_at=completed_at,
                )
                # No ORM relationship is declared deliberately; flush the immutable proposal first
                # so both scoped foreign keys are satisfied independent of unit-of-work ordering.
                session.add(send_proposal)
                await session.flush()
                session.add_all((approval, managed_draft))
                await session.flush()
                return CreateDraftCompletion(
                    managed_draft=_managed_draft_record(managed_draft),
                    send_proposal=_proposal_record(send_proposal),
                    approval=_approval_record(approval),
                )

    async def complete_update_draft(
        self,
        claim: ActionClaim,
        *,
        provider_draft_id: str,
        provider_message_id: str | None,
        provider_thread_id: str | None,
        callback_token_digest: str,
        telegram_account_id: UUID,
        provider_chat_id: int,
        approval_expires_at: datetime,
        completed_at: datetime,
    ) -> CreateDraftCompletion:
        async with self._database.session() as session:
            async with session.begin():
                values = (
                    await session.execute(
                        select(Action, ActionProposal)
                        .join(
                            ActionProposal,
                            and_(
                                ActionProposal.id == Action.proposal_id,
                                ActionProposal.workspace_id == Action.workspace_id,
                                ActionProposal.user_id == Action.user_id,
                            ),
                        )
                        .where(_claim_predicate(claim))
                        .with_for_update()
                    )
                ).one_or_none()
                if values is None:
                    raise ActionConflictError("action claim is stale")
                action_row, update_proposal = values
                if (
                    action_row.capability != GmailActionCapability.UPDATE_DRAFT
                    or update_proposal.supersedes_proposal_id is None
                ):
                    raise ActionValidationError("action is not a managed Gmail draft update")
                managed_draft = await session.scalar(
                    select(ManagedGmailDraft)
                    .where(
                        ManagedGmailDraft.active_send_proposal_id
                        == update_proposal.supersedes_proposal_id,
                        ManagedGmailDraft.workspace_id == claim.workspace_id,
                        ManagedGmailDraft.user_id == claim.user_id,
                    )
                    .with_for_update()
                )
                if managed_draft is None or managed_draft.provider_draft_id != provider_draft_id:
                    raise ActionValidationError("managed Gmail draft is unavailable")

                message = CanonicalEmail.model_validate(update_proposal.parameters_json)
                action_row.status = ActionStatus.SUCCEEDED
                action_row.finished_at = completed_at
                action_row.claim_id = None
                action_row.lease_expires_at = None
                update_proposal.status = ActionProposalStatus.COMPLETED
                update_proposal.terminal_at = completed_at
                session.add(
                    ActionResult(
                        action_id=action_row.id,
                        user_id=action_row.user_id,
                        workspace_id=action_row.workspace_id,
                        outcome=ActionStatus.SUCCEEDED,
                        provider_status_category="success",
                        provider_draft_id=provider_draft_id,
                        provider_message_id=provider_message_id,
                        provider_thread_id=provider_thread_id,
                        result_metadata={},
                        created_at=completed_at,
                    )
                )

                send_version = managed_draft.send_proposal_version + 1
                send_source_key = f"managed-draft:{managed_draft.id}:send:{send_version}"
                send_proposal_id = uuid5(
                    _PROPOSAL_NAMESPACE,
                    f"{action_row.workspace_id}:{action_row.user_id}:{send_source_key}",
                )
                send_proposal = ActionProposal(
                    id=send_proposal_id,
                    user_id=action_row.user_id,
                    workspace_id=action_row.workspace_id,
                    connector_account_id=update_proposal.connector_account_id,
                    event_id=update_proposal.event_id,
                    situation_id=update_proposal.situation_id,
                    goal_id=update_proposal.goal_id,
                    agent_run_id=update_proposal.agent_run_id,
                    conversation_turn_id=update_proposal.conversation_turn_id,
                    supersedes_proposal_id=update_proposal.supersedes_proposal_id,
                    source_key=send_source_key,
                    proposal_family_key=f"managed-draft:{managed_draft.id}:send",
                    origin=update_proposal.origin,
                    capability=GmailActionCapability.SEND_DRAFT,
                    parameters_json=message.model_dump(mode="json"),
                    parameters_hash=update_proposal.parameters_hash,
                    description="Send the exact revised managed Gmail draft",
                    risk_level="high",
                    policy_decision=PolicyDecision.REQUIRE_APPROVAL,
                    status=ActionProposalStatus.WAITING_APPROVAL,
                    version=send_version,
                    created_at=completed_at,
                    expires_at=approval_expires_at,
                )
                approval = ActionApproval(
                    id=uuid5(_APPROVAL_NAMESPACE, str(send_proposal_id)),
                    proposal_id=send_proposal_id,
                    user_id=action_row.user_id,
                    workspace_id=action_row.workspace_id,
                    parameters_hash=update_proposal.parameters_hash,
                    principal_type="TELEGRAM",
                    telegram_account_id=telegram_account_id,
                    provider_chat_id=provider_chat_id,
                    status=ApprovalStatus.PENDING,
                    callback_token_digest=callback_token_digest,
                    requested_at=completed_at,
                    expires_at=approval_expires_at,
                )
                session.add(send_proposal)
                await session.flush()
                managed_draft.provider_message_id = provider_message_id
                managed_draft.gmail_thread_id = provider_thread_id
                managed_draft.active_send_proposal_id = send_proposal_id
                managed_draft.send_proposal_version = send_version
                managed_draft.current_content_hash = update_proposal.parameters_hash
                managed_draft.status = ManagedDraftStatus.READY
                managed_draft.updated_at = completed_at
                session.add(approval)
                await session.flush()
                return CreateDraftCompletion(
                    managed_draft=_managed_draft_record(managed_draft),
                    send_proposal=_proposal_record(send_proposal),
                    approval=_approval_record(approval),
                )

    async def complete_delete_draft(
        self,
        claim: ActionClaim,
        *,
        provider_draft_id: str,
        completed_at: datetime,
    ) -> None:
        await self._complete_managed_action(
            claim,
            provider_draft_id=provider_draft_id,
            provider_message_id=None,
            provider_thread_id=None,
            managed_status=ManagedDraftStatus.DELETED,
            completed_at=completed_at,
        )

    async def complete_send_draft(
        self,
        claim: ActionClaim,
        *,
        provider_draft_id: str,
        provider_message_id: str,
        provider_thread_id: str,
        completed_at: datetime,
    ) -> None:
        await self._complete_managed_action(
            claim,
            provider_draft_id=provider_draft_id,
            provider_message_id=provider_message_id,
            provider_thread_id=provider_thread_id,
            managed_status=ManagedDraftStatus.SENT,
            completed_at=completed_at,
        )

    async def _complete_managed_action(
        self,
        claim: ActionClaim,
        *,
        provider_draft_id: str,
        provider_message_id: str | None,
        provider_thread_id: str | None,
        managed_status: ManagedDraftStatus,
        completed_at: datetime,
    ) -> None:
        async with self._database.session() as session:
            async with session.begin():
                values = (
                    await session.execute(
                        select(Action, ActionProposal)
                        .join(
                            ActionProposal,
                            and_(
                                ActionProposal.id == Action.proposal_id,
                                ActionProposal.workspace_id == Action.workspace_id,
                                ActionProposal.user_id == Action.user_id,
                            ),
                        )
                        .where(_claim_predicate(claim))
                        .with_for_update()
                    )
                ).one_or_none()
                if values is None:
                    raise ActionConflictError("action claim is stale")
                action, proposal = values
                managed_predicate = (
                    ManagedGmailDraft.active_send_proposal_id == proposal.id
                    if claim.capability == GmailActionCapability.SEND_DRAFT
                    else ManagedGmailDraft.active_send_proposal_id
                    == proposal.supersedes_proposal_id
                )
                managed = await session.scalar(
                    select(ManagedGmailDraft)
                    .where(
                        managed_predicate,
                        ManagedGmailDraft.workspace_id == claim.workspace_id,
                        ManagedGmailDraft.user_id == claim.user_id,
                    )
                    .with_for_update()
                )
                if managed is None or managed.provider_draft_id != provider_draft_id:
                    raise ActionValidationError("managed Gmail draft is unavailable")
                action.status = ActionStatus.SUCCEEDED
                action.finished_at = completed_at
                action.claim_id = None
                action.lease_expires_at = None
                proposal.status = ActionProposalStatus.COMPLETED
                proposal.terminal_at = completed_at
                managed.status = managed_status
                managed.updated_at = completed_at
                if managed_status == ManagedDraftStatus.SENT:
                    managed.provider_message_id = provider_message_id
                    managed.gmail_thread_id = provider_thread_id
                    managed.sent_at = completed_at
                elif managed_status == ManagedDraftStatus.DELETED:
                    managed.deleted_at = completed_at
                session.add(
                    ActionResult(
                        action_id=action.id,
                        user_id=action.user_id,
                        workspace_id=action.workspace_id,
                        outcome=ActionStatus.SUCCEEDED,
                        provider_status_category="success",
                        provider_draft_id=provider_draft_id,
                        provider_message_id=provider_message_id,
                        provider_thread_id=provider_thread_id,
                        result_metadata={},
                        created_at=completed_at,
                    )
                )

    async def grant_and_queue_send(
        self,
        *,
        callback_token_digest: str,
        telegram_account_id: UUID,
        chat_id: int,
        destination: str,
        now: datetime,
    ) -> ApprovalDecision:
        async with self._database.session() as session:
            async with session.begin():
                values = (
                    await session.execute(
                        select(ActionApproval, ActionProposal)
                        .join(
                            ActionProposal,
                            and_(
                                ActionProposal.id == ActionApproval.proposal_id,
                                ActionProposal.workspace_id == ActionApproval.workspace_id,
                                ActionProposal.user_id == ActionApproval.user_id,
                            ),
                        )
                        .where(ActionApproval.callback_token_digest == callback_token_digest)
                        .with_for_update()
                    )
                ).one_or_none()
                if values is None:
                    raise ActionScopeError("approval is unavailable")
                approval, proposal = values
                if (
                    approval.telegram_account_id != telegram_account_id
                    or approval.provider_chat_id != chat_id
                ):
                    raise ActionScopeError("approval is unavailable")
                existing_action = await session.scalar(
                    select(Action).where(Action.proposal_id == proposal.id)
                )
                if approval.status == ApprovalStatus.GRANTED and existing_action is not None:
                    return ApprovalDecision(
                        outcome=ApprovalDecisionOutcome.ALREADY_DECIDED,
                        approval=_approval_record(approval),
                        action=_action_record(existing_action),
                    )
                if approval.status != ApprovalStatus.PENDING:
                    return ApprovalDecision(
                        outcome=ApprovalDecisionOutcome.ALREADY_DECIDED,
                        approval=_approval_record(approval),
                        action=None if existing_action is None else _action_record(existing_action),
                    )
                if now >= approval.expires_at:
                    approval.status = ApprovalStatus.EXPIRED
                    approval.decided_at = now
                    proposal.status = ActionProposalStatus.EXPIRED
                    proposal.terminal_at = now
                    await session.flush()
                    return ApprovalDecision(
                        outcome=ApprovalDecisionOutcome.EXPIRED,
                        approval=_approval_record(approval),
                    )
                managed_draft = await session.scalar(
                    select(ManagedGmailDraft)
                    .where(
                        ManagedGmailDraft.active_send_proposal_id == proposal.id,
                        ManagedGmailDraft.workspace_id == proposal.workspace_id,
                        ManagedGmailDraft.user_id == proposal.user_id,
                    )
                    .with_for_update()
                )
                if (
                    managed_draft is None
                    or managed_draft.current_content_hash != proposal.parameters_hash
                    or approval.parameters_hash != proposal.parameters_hash
                ):
                    approval.status = ApprovalStatus.SUPERSEDED
                    approval.decided_at = now
                    proposal.status = ActionProposalStatus.SUPERSEDED
                    proposal.terminal_at = now
                    await session.flush()
                    return ApprovalDecision(
                        outcome=ApprovalDecisionOutcome.STALE,
                        approval=_approval_record(approval),
                    )

                approval.status = ApprovalStatus.GRANTED
                approval.decided_at = now
                proposal.status = ActionProposalStatus.APPROVED
                action = await self._queue_action_in_session(
                    session,
                    proposal=proposal,
                    destination=destination,
                    queued_at=now,
                )
                managed_draft.status = ManagedDraftStatus.SENDING
                managed_draft.updated_at = now
                await session.flush()
                return ApprovalDecision(
                    outcome=ApprovalDecisionOutcome.QUEUED,
                    approval=_approval_record(approval),
                    action=_action_record(action),
                )

    async def supersede_send_approval(
        self,
        *,
        callback_token_digest: str,
        telegram_account_id: UUID,
        chat_id: int,
        now: datetime,
    ) -> ApprovalRecord:
        async with self._database.session() as session:
            async with session.begin():
                values = (
                    await session.execute(
                        select(ActionApproval, ActionProposal)
                        .join(
                            ActionProposal,
                            and_(
                                ActionProposal.id == ActionApproval.proposal_id,
                                ActionProposal.workspace_id == ActionApproval.workspace_id,
                                ActionProposal.user_id == ActionApproval.user_id,
                            ),
                        )
                        .where(ActionApproval.callback_token_digest == callback_token_digest)
                        .with_for_update()
                    )
                ).one_or_none()
                if values is None:
                    raise ActionScopeError("approval is unavailable")
                approval, proposal = values
                if (
                    approval.telegram_account_id != telegram_account_id
                    or approval.provider_chat_id != chat_id
                ):
                    raise ActionScopeError("approval is unavailable")
                if approval.status == ApprovalStatus.PENDING:
                    approval.status = ApprovalStatus.SUPERSEDED
                    approval.decided_at = now
                    proposal.status = ActionProposalStatus.SUPERSEDED
                    proposal.terminal_at = now
                    await session.flush()
                return _approval_record(approval)

    async def get_action(self, action_id: UUID) -> ActionRecord | None:
        async with self._database.session() as session:
            row = await session.scalar(select(Action).where(Action.id == action_id))
        return None if row is None else _action_record(row)

    async def get_approval(
        self,
        *,
        approval_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
    ) -> ApprovalRecord | None:
        async with self._database.session() as session:
            row = await session.scalar(
                select(ActionApproval).where(
                    ActionApproval.id == approval_id,
                    ActionApproval.user_id == user_id,
                    ActionApproval.workspace_id == workspace_id,
                )
            )
        return None if row is None else _approval_record(row)

    async def _queue_action_in_session(
        self,
        session: AsyncSession,
        *,
        proposal: ActionProposal,
        destination: str,
        queued_at: datetime,
    ) -> Action:
        action_id = uuid5(_ACTION_NAMESPACE, f"{proposal.id}:{proposal.capability}")
        idempotency_key = f"proposal:{proposal.id}:{proposal.capability}"
        action = Action(
            id=action_id,
            proposal_id=proposal.id,
            event_id=proposal.event_id,
            user_id=proposal.user_id,
            workspace_id=proposal.workspace_id,
            capability=proposal.capability,
            idempotency_key=idempotency_key,
            status=ActionStatus.QUEUED,
            created_at=queued_at,
        )
        session.add(action)
        outbox_id = uuid5(_OUTBOX_NAMESPACE, str(action_id))
        envelope = ActionExecutionRequestedMessage(
            outbox_message_id=outbox_id,
            action_id=action_id,
            user_id=proposal.user_id,
            workspace_id=proposal.workspace_id,
        )
        session.add(
            OutboxMessage(
                id=outbox_id,
                event_id=proposal.event_id,
                destination=destination,
                message_type=envelope.message_type,
                schema_version=envelope.schema_version,
                payload=envelope.model_dump(mode="json"),
                state=OutboxState.PENDING,
                available_at=queued_at,
            )
        )
        return action


def _claim_predicate(claim: ActionClaim) -> Any:
    return and_(
        Action.id == claim.action_id,
        Action.proposal_id == claim.proposal_id,
        Action.user_id == claim.user_id,
        Action.workspace_id == claim.workspace_id,
        Action.status == ActionStatus.RUNNING,
        Action.claim_id == claim.claim_id,
    )


def _proposal_record(row: ActionProposal) -> ActionProposalRecord:
    return ActionProposalRecord(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        connector_account_id=row.connector_account_id,
        event_id=row.event_id,
        situation_id=row.situation_id,
        goal_id=row.goal_id,
        agent_run_id=row.agent_run_id,
        conversation_turn_id=row.conversation_turn_id,
        supersedes_proposal_id=row.supersedes_proposal_id,
        source_key=row.source_key,
        proposal_family_key=row.proposal_family_key,
        origin=row.origin,
        capability=row.capability,
        message=CanonicalEmail.model_validate(row.parameters_json),
        parameters_hash=row.parameters_hash,
        description=row.description,
        risk_level=row.risk_level,
        policy_decision=row.policy_decision,
        status=row.status,
        version=row.version,
        created_at=row.created_at,
        expires_at=row.expires_at,
        terminal_at=row.terminal_at,
    )


def _action_record(row: Action) -> ActionRecord:
    return ActionRecord(
        id=row.id,
        proposal_id=row.proposal_id,
        event_id=row.event_id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        capability=row.capability,
        idempotency_key=row.idempotency_key,
        cloud_task_name=row.cloud_task_name,
        status=row.status,
        claim_id=row.claim_id,
        lease_expires_at=row.lease_expires_at,
        attempt_count=row.attempt_count,
        provider_call_started_at=row.provider_call_started_at,
        failure_code=row.failure_code,
        failure_summary=row.failure_summary,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _approval_record(row: ActionApproval) -> ApprovalRecord:
    return ApprovalRecord(
        id=row.id,
        proposal_id=row.proposal_id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        parameters_hash=row.parameters_hash,
        principal_type=row.principal_type,
        telegram_account_id=row.telegram_account_id,
        provider_chat_id=row.provider_chat_id,
        status=row.status,
        requested_at=row.requested_at,
        decided_at=row.decided_at,
        expires_at=row.expires_at,
    )


def _managed_draft_record(row: ManagedGmailDraft) -> ManagedGmailDraftRecord:
    return ManagedGmailDraftRecord(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        connector_account_id=row.connector_account_id,
        provider_draft_id=row.provider_draft_id,
        provider_message_id=row.provider_message_id,
        gmail_thread_id=row.gmail_thread_id,
        create_proposal_id=row.create_proposal_id,
        active_send_proposal_id=row.active_send_proposal_id,
        send_proposal_version=row.send_proposal_version,
        current_content_hash=row.current_content_hash,
        rfc_message_id=row.rfc_message_id,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        sent_at=row.sent_at,
        deleted_at=row.deleted_at,
    )
