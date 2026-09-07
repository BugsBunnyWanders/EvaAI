from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eva_ai.agent.types import AgentUsage, ProposedAction, ToolCallAudit
from eva_ai.memory.types import AgentWorkingContext, MemoryProposal, MemorySourceType


def _required_text(value: object) -> object:
    if isinstance(value, str):
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("must not be blank")
        return normalized
    return value


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


class ConversationKind(StrEnum):
    GENERAL = "GENERAL"
    SITUATION = "SITUATION"


class ConversationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class ConversationTurnRole(StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


class ConversationTurnStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


class ConversationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    user_id: UUID
    workspace_id: UUID
    telegram_account_id: UUID
    situation_id: UUID
    kind: ConversationKind
    status: ConversationStatus
    summary: str = Field(max_length=4000)
    next_sequence: int = Field(ge=1)
    last_activity_at: datetime
    created_at: datetime
    updated_at: datetime

    _require_aware = field_validator("last_activity_at", "created_at", "updated_at")(_aware)


class ConversationTurnRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    user_id: UUID
    workspace_id: UUID
    conversation_id: UUID
    role: ConversationTurnRole
    status: ConversationTurnStatus
    sequence: int = Field(ge=1)
    text: str = Field(max_length=4000)
    event_id: UUID | None
    notification_id: UUID | None
    agent_version: str | None = Field(default=None, max_length=100)
    attempt_count: int = Field(ge=0)
    next_retry_at: datetime | None
    claim_id: UUID | None
    lease_expires_at: datetime | None
    provider_response_id: str | None = Field(default=None, max_length=500)
    usage: AgentUsage
    tool_audit: tuple[ToolCallAudit, ...]
    reasoning_summary: str | None = Field(default=None, max_length=2000)
    proposed_actions: tuple[ProposedAction, ...] = ()
    memory_proposals: tuple[MemoryProposal, ...] = ()
    failure_code: str | None = Field(default=None, max_length=100)
    failure_summary: str | None = Field(default=None, max_length=500)
    created_at: datetime
    completed_at: datetime | None

    _require_aware = field_validator(
        "next_retry_at", "lease_expires_at", "created_at", "completed_at"
    )(_aware)


class ConversationHistoryTurn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ConversationTurnRole
    text: str = Field(max_length=4000)


class ConversationAgentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: UUID
    conversation_id: UUID
    message: str = Field(max_length=4000)
    history: tuple[ConversationHistoryTurn, ...] = Field(default=(), max_length=40)
    context: AgentWorkingContext
    is_email_situation: bool


class ConversationAgentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message: str = Field(max_length=4000)
    reasoning_summary: str = Field(max_length=2000)
    proposed_actions: tuple[ProposedAction, ...] = Field(default=(), max_length=10)
    memory_proposals: tuple[MemoryProposal, ...] = Field(default=(), max_length=10)

    _normalize = field_validator("message", "reasoning_summary", mode="before")(_required_text)

    @field_validator("memory_proposals")
    @classmethod
    def prevent_user_provenance(
        cls, values: tuple[MemoryProposal, ...]
    ) -> tuple[MemoryProposal, ...]:
        if any(
            value.source_type
            not in {MemorySourceType.AGENT_INFERRED, MemorySourceType.EXTERNAL_EVENT}
            for value in values
        ):
            raise ValueError("agent memory proposals cannot claim user provenance")
        return values


class ConversationInvocationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    result: ConversationAgentResult
    provider_response_id: str | None = Field(default=None, max_length=500)
    usage: AgentUsage = Field(default_factory=AgentUsage)
    tool_audit: tuple[ToolCallAudit, ...] = ()


class ConversationOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    TERMINAL = "TERMINAL"
    RETRY = "RETRY"
    DEFERRED = "DEFERRED"
