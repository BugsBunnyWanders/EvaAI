from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from eva_ai.actions.types import (
    ActionClaimResult,
    ActionOrigin,
    ActionProposalPreparation,
    ActionTaskRequest,
    AllowedActionCreation,
    NewActionProposal,
    PreparedActionProposal,
)
from eva_ai.agent.contracts import GmailInvestigationReader
from eva_ai.agent.types import ProposedAction


class ActionStore(Protocol):
    async def create_allowed_action(
        self,
        *,
        proposal: NewActionProposal,
        destination: str,
    ) -> AllowedActionCreation: ...

    async def claim_action(
        self,
        request: ActionTaskRequest,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> ActionClaimResult: ...


class ActionProposalSessionStore(Protocol):
    async def create_allowed_action_in_session(
        self,
        session: AsyncSession,
        *,
        proposal: NewActionProposal,
        destination: str,
    ) -> AllowedActionCreation: ...


class ActionProposalPreparer(Protocol):
    async def prepare_model_proposals(
        self,
        proposals: tuple[ProposedAction, ...],
        *,
        reader: GmailInvestigationReader | None,
        allowed_thread_id: str | None,
        account_identity: str,
    ) -> ActionProposalPreparation: ...


class ActionProposalWriter(Protocol):
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
    ) -> tuple[AllowedActionCreation, ...]: ...
