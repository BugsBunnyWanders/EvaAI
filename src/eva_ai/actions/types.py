from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GmailActionCapability(StrEnum):
    CREATE_DRAFT = "gmail.create_draft"
    UPDATE_DRAFT = "gmail.update_draft"
    SEND_DRAFT = "gmail.send_draft"
    DELETE_DRAFT = "gmail.delete_draft"


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

