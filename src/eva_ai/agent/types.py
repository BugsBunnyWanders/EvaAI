import json
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from eva_ai.memory.types import AgentWorkingContext, MemoryProposal, MemorySourceType


def _required_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


class AgentRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


class AgentDecision(StrEnum):
    NO_ACTION = "NO_ACTION"
    NOTIFY_USER = "NOTIFY_USER"
    ASK_USER = "ASK_USER"
    PROPOSE_ACTION = "PROPOSE_ACTION"
    FOLLOW_UP = "FOLLOW_UP"


class NotificationUrgency(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SituationUpdateProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    current_state: str | None = Field(default=None, max_length=100)
    summary: str | None = Field(default=None, max_length=2000)
    next_action: str | None = Field(default=None, max_length=1000)
    next_expected: str | None = Field(default=None, max_length=1000)

    _normalize = field_validator(
        "current_state", "summary", "next_action", "next_expected", mode="before"
    )(lambda value: None if value is None else _required_text(value))

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if not any(
            value is not None
            for value in (self.current_state, self.summary, self.next_action, self.next_expected)
        ):
            raise ValueError("situation update must propose at least one change")
        return self


class ProposedAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: str = Field(max_length=100)
    description: str = Field(max_length=1000)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    requires_approval: bool = True

    _normalize = field_validator("capability", "description", mode="before")(_required_text)

    @field_validator("arguments")
    @classmethod
    def bound_arguments(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        # Bound nested JSON after canonical serialization; limiting only the number of top-level
        # keys would still permit an arbitrarily large model-produced proposal.
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > 8_000:
            raise ValueError("action arguments exceed the serialized size limit")
        return value


class NotificationProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    urgency: NotificationUrgency
    message: str = Field(max_length=1500)

    _normalize = field_validator("message", mode="before")(_required_text)


class FollowUpProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instruction: str = Field(max_length=1000)
    not_before: datetime | None = None

    _normalize = field_validator("instruction", mode="before")(_required_text)
    _require_aware = field_validator("not_before")(_aware)


class AgentInvestigationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    situation_update: SituationUpdateProposal | None = None
    decision: AgentDecision
    reasoning_summary: str = Field(max_length=2000)
    proposed_actions: tuple[ProposedAction, ...] = Field(default=(), max_length=10)
    notification: NotificationProposal | None = None
    memory_proposals: tuple[MemoryProposal, ...] = Field(default=(), max_length=10)
    follow_up: FollowUpProposal | None = None

    _normalize = field_validator("reasoning_summary", mode="before")(_required_text)

    @model_validator(mode="after")
    def validate_decision_payload(self) -> Self:
        if self.decision in {AgentDecision.NOTIFY_USER, AgentDecision.ASK_USER}:
            if self.notification is None:
                raise ValueError("notification is required for a user-facing decision")
        if self.decision is AgentDecision.PROPOSE_ACTION and not self.proposed_actions:
            raise ValueError("PROPOSE_ACTION requires at least one action")
        if self.decision is AgentDecision.FOLLOW_UP and self.follow_up is None:
            raise ValueError("FOLLOW_UP requires follow_up")
        if any(
            proposal.source_type
            not in {MemorySourceType.AGENT_INFERRED, MemorySourceType.EXTERNAL_EVENT}
            for proposal in self.memory_proposals
        ):
            raise ValueError("agent memory proposals cannot claim user provenance")
        return self


class AgentRunRequestedMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_type: Literal["agent.run.requested"] = "agent.run.requested"
    outbox_message_id: UUID
    agent_run_id: UUID
    event_id: UUID
    signal_id: UUID
    situation_id: UUID
    user_id: UUID
    workspace_id: UUID
    schema_version: int = Field(default=1, gt=0)


class ToolCallAudit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: Literal["gmail_read_thread", "gmail_search"]
    duration_ms: int = Field(ge=0)
    outcome: Literal["succeeded", "failed", "budget_exhausted"]
    result_count: int = Field(default=0, ge=0, le=20)


class AgentUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class AgentRunRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    user_id: UUID
    workspace_id: UUID
    event_id: UUID
    signal_id: UUID
    situation_id: UUID
    status: AgentRunStatus
    provider: str = Field(max_length=50)
    model: str = Field(max_length=100)
    agent_version: str = Field(max_length=100)
    prompt_version: str = Field(max_length=100)
    output_schema_version: int = Field(ge=1)
    attempt_count: int = Field(ge=0)
    next_retry_at: datetime | None
    claim_id: UUID | None
    lease_expires_at: datetime | None
    input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result: AgentInvestigationResult | None
    provider_response_id: str | None = Field(default=None, max_length=500)
    usage: AgentUsage
    tool_audit: tuple[ToolCallAudit, ...]
    failure_code: str | None = Field(default=None, max_length=100)
    failure_summary: str | None = Field(default=None, max_length=500)
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    _require_aware = field_validator(
        "next_retry_at", "lease_expires_at", "queued_at", "started_at", "completed_at"
    )(_aware)


class AgentEventContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    event_type: str
    occurred_at: datetime
    sender: str = Field(max_length=500)
    subject: str = Field(max_length=500)
    snippet: str = Field(max_length=1000)
    plain_text: str = Field(max_length=6000)
    label_ids: tuple[str, ...] = Field(default=(), max_length=100)
    attachments: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=50)

    _require_aware = field_validator("occurred_at")(_aware)


class AgentInvocationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_run_id: UUID
    signal_id: UUID
    event: AgentEventContext
    context: AgentWorkingContext
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class GmailMessageEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str = Field(max_length=500)
    thread_id: str = Field(max_length=500)
    internal_date: datetime
    sender: str = Field(max_length=500)
    recipients: str = Field(max_length=1000)
    subject: str = Field(max_length=500)
    snippet: str = Field(max_length=1000)
    plain_text: str = Field(max_length=6000)
    label_ids: tuple[str, ...] = Field(default=(), max_length=100)
    attachments: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=50)

    _require_aware = field_validator("internal_date")(_aware)


class GmailThreadEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    thread_id: str = Field(max_length=500)
    messages: tuple[GmailMessageEvidence, ...] = Field(max_length=20)
    truncated: bool


class GmailSearchEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[GmailMessageEvidence, ...] = Field(max_length=10)
    truncated: bool


class InvestigationOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    TERMINAL = "TERMINAL"
    RETRY = "RETRY"
    DEFERRED = "DEFERRED"
