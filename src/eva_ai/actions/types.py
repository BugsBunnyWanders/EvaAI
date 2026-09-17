from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eva_ai.actions.canonical import CanonicalEmail, canonical_email_hash


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


class GmailActionCapability(StrEnum):
    CREATE_DRAFT = "gmail.create_draft"
    UPDATE_DRAFT = "gmail.update_draft"
    SEND_DRAFT = "gmail.send_draft"
    DELETE_DRAFT = "gmail.delete_draft"


@dataclass(frozen=True, slots=True)
class PreparedActionProposal:
    capability: GmailActionCapability
    description: str
    message: CanonicalEmail


@dataclass(frozen=True, slots=True)
class ActionProposalPreparation:
    proposals: tuple[PreparedActionProposal, ...] = ()
    clarification: str | None = None


class ActionOrigin(StrEnum):
    AGENT_RUN = "AGENT_RUN"
    TELEGRAM_USER = "TELEGRAM_USER"
    SYSTEM = "SYSTEM"


class PolicyDecision(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class ActionProposalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    QUEUED = "QUEUED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    GRANTED = "GRANTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class ActionStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ManagedDraftStatus(StrEnum):
    CREATING = "CREATING"
    READY = "READY"
    UPDATING = "UPDATING"
    SENDING = "SENDING"
    SENT = "SENT"
    DELETING = "DELETING"
    DELETED = "DELETED"
    UNKNOWN = "UNKNOWN"


class RevisionSessionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class ActionClaimOutcome(StrEnum):
    CLAIMED = "CLAIMED"
    BUSY = "BUSY"
    TERMINAL = "TERMINAL"
    UNKNOWN = "UNKNOWN"


class ApprovalDecisionOutcome(StrEnum):
    QUEUED = "QUEUED"
    ALREADY_DECIDED = "ALREADY_DECIDED"
    EXPIRED = "EXPIRED"
    STALE = "STALE"


class ApprovalCallbackOutcome(StrEnum):
    QUEUED = "QUEUED"
    ALREADY_DECIDED = "ALREADY_DECIDED"
    EXPIRED = "EXPIRED"
    STALE = "STALE"
    CHANGE_REQUESTED = "CHANGE_REQUESTED"


class ActionExecutionOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    TERMINAL = "TERMINAL"
    RETRY = "RETRY"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"


class NewActionProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: UUID
    workspace_id: UUID
    connector_account_id: UUID
    event_id: UUID
    situation_id: UUID | None = None
    goal_id: UUID | None = None
    agent_run_id: UUID | None = None
    conversation_turn_id: UUID | None = None
    supersedes_proposal_id: UUID | None = None
    origin: ActionOrigin
    capability: GmailActionCapability
    message: CanonicalEmail
    description: str = Field(min_length=1, max_length=1000)
    risk_level: Literal["low", "medium", "high"] = "low"
    policy_decision: PolicyDecision
    source_key: str = Field(min_length=1, max_length=500)
    proposal_family_key: str = Field(min_length=1, max_length=500)
    version: int = Field(default=1, ge=1)
    created_at: datetime
    expires_at: datetime | None = None

    _require_aware = field_validator("created_at", "expires_at")(_aware)

    @model_validator(mode="after")
    def validate_expiry(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must follow created_at")
        return self

    @property
    def message_hash(self) -> str:
        return canonical_email_hash(self.message)

    @property
    def parameters_hash(self) -> str:
        return self.message_hash


class ActionProposalRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    user_id: UUID
    workspace_id: UUID
    connector_account_id: UUID
    event_id: UUID
    situation_id: UUID | None
    goal_id: UUID | None
    agent_run_id: UUID | None
    conversation_turn_id: UUID | None
    supersedes_proposal_id: UUID | None
    source_key: str
    proposal_family_key: str
    origin: ActionOrigin
    capability: GmailActionCapability
    message: CanonicalEmail
    parameters_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str
    risk_level: str
    policy_decision: PolicyDecision
    status: ActionProposalStatus
    version: int = Field(ge=1)
    created_at: datetime
    expires_at: datetime | None
    terminal_at: datetime | None

    _require_aware = field_validator("created_at", "expires_at", "terminal_at")(_aware)


class ActionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    proposal_id: UUID
    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    capability: GmailActionCapability
    idempotency_key: str
    cloud_task_name: str | None
    status: ActionStatus
    claim_id: UUID | None
    lease_expires_at: datetime | None
    attempt_count: int = Field(ge=0)
    provider_call_started_at: datetime | None
    failure_code: str | None
    failure_summary: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    _require_aware = field_validator(
        "lease_expires_at",
        "provider_call_started_at",
        "created_at",
        "started_at",
        "finished_at",
    )(_aware)


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    proposal_id: UUID
    user_id: UUID
    workspace_id: UUID
    parameters_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    principal_type: str
    telegram_account_id: UUID
    provider_chat_id: int
    status: ApprovalStatus
    requested_at: datetime
    decided_at: datetime | None
    expires_at: datetime

    _require_aware = field_validator("requested_at", "decided_at", "expires_at")(_aware)


class ManagedGmailDraftRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    user_id: UUID
    workspace_id: UUID
    connector_account_id: UUID
    provider_draft_id: str
    provider_message_id: str | None
    gmail_thread_id: str | None
    create_proposal_id: UUID
    active_send_proposal_id: UUID | None
    send_proposal_version: int = Field(ge=0)
    current_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rfc_message_id: str
    status: ManagedDraftStatus
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None
    deleted_at: datetime | None

    _require_aware = field_validator("created_at", "updated_at", "sent_at", "deleted_at")(_aware)


class AllowedActionCreation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal: ActionProposalRecord
    action: ActionRecord


class CreateDraftCompletion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    managed_draft: ManagedGmailDraftRecord
    send_proposal: ActionProposalRecord
    approval: ApprovalRecord


class ActionClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: UUID
    proposal_id: UUID
    claim_id: UUID
    user_id: UUID
    workspace_id: UUID
    capability: GmailActionCapability
    attempt_count: int = Field(ge=1)


class ActionClaimResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: ActionClaimOutcome
    claim: ActionClaim | None = None


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: ApprovalDecisionOutcome
    approval: ApprovalRecord
    action: ActionRecord | None = None


class ApprovalCallbackResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: ApprovalCallbackOutcome
    action_id: UUID | None = None


class ActionExecutionRequestedMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_type: Literal["action.execution.requested"] = "action.execution.requested"
    outbox_message_id: UUID
    action_id: UUID
    user_id: UUID
    workspace_id: UUID
    schema_version: int = Field(default=1, gt=0)


class ActionTaskRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: UUID
    schema_version: int = Field(default=1, gt=0)


class TelegramApprovalPrincipal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    telegram_account_id: UUID
    provider_chat_id: int


class ActionExecutionSubject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: ActionClaim
    proposal: ActionProposalRecord
    connector_id: UUID
    connector_identity: str
    connector_status: str
    connector_scopes: tuple[str, ...]
    secret_reference: str | None
    managed_draft: ManagedGmailDraftRecord | None = None
    approval: ApprovalRecord | None = None
    telegram_principal: TelegramApprovalPrincipal | None = None


class ActionExecutionResult(BaseModel):
    """Sanitized executor result safe for Cloud Tasks HTTP responses and logs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: UUID
    outcome: ActionExecutionOutcome
    provider_status_category: str | None = Field(default=None, max_length=100)
