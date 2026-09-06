from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID, uuid7

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eva_ai.situations.types import AttentionLevel, GoalContribution


class RelevanceDisposition(StrEnum):
    IGNORE = "IGNORE"
    RECORD = "RECORD"
    NOTIFY = "NOTIFY"
    INVESTIGATE = "INVESTIGATE"


class RelevanceProvider(StrEnum):
    OPENAI = "openai"


class RelevanceCategory(StrEnum):
    GOAL_PROGRESS = "GOAL_PROGRESS"
    REQUEST_OR_COMMITMENT = "REQUEST_OR_COMMITMENT"
    DEADLINE = "DEADLINE"
    RISK_OR_SECURITY = "RISK_OR_SECURITY"
    FINANCIAL = "FINANCIAL"
    TRAVEL = "TRAVEL"
    PERSONAL = "PERSONAL"
    INFORMATIONAL = "INFORMATIONAL"
    PROMOTIONAL = "PROMOTIONAL"
    SPAM = "SPAM"
    OTHER = "OTHER"


class SignalKind(StrEnum):
    RELEVANCE = "RELEVANCE"


class SignalProducer(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    AI = "AI"


class EvaluationTrigger(StrEnum):
    INITIAL = "INITIAL"
    EXPLICIT_REEVALUATION = "EXPLICIT_REEVALUATION"
    BACKFILL = "BACKFILL"


class EvaluationAttemptStatus(StrEnum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


class ScreeningReason(StrEnum):
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    MALFORMED_EVENT = "MALFORMED_EVENT"
    UNSUPPORTED_EVENT = "UNSUPPORTED_EVENT"
    IGNORED_SOURCE = "IGNORED_SOURCE"
    IGNORED_EVENT_TYPE = "IGNORED_EVENT_TYPE"
    IGNORED_SENDER = "IGNORED_SENDER"
    IGNORED_LABEL = "IGNORED_LABEL"


class RelevanceMethod(StrEnum):
    AI = "AI"
    DETERMINISTIC = "DETERMINISTIC"


def _required_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


def _optional_text(value: object) -> object:
    return None if value is None else _required_text(value)


def _trim_text(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


class GoalMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_id: UUID
    relevance: float = Field(ge=0.0, le=1.0)
    contribution: GoalContribution
    reason: str = Field(min_length=1, max_length=500)

    _normalize_reason = field_validator("reason", mode="before")(_required_text)


class ClassifierResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    relevance: float = Field(ge=0.0, le=1.0)
    importance: float = Field(ge=0.0, le=1.0)
    urgency: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    category: RelevanceCategory
    recommended_action: RelevanceDisposition
    reason: str = Field(min_length=1, max_length=1000)
    goal_matches: tuple[GoalMatch, ...] = Field(default=(), max_length=5)

    _normalize_reason = field_validator("reason", mode="before")(_required_text)

    @field_validator("goal_matches")
    @classmethod
    def normalize_goal_matches(cls, values: tuple[GoalMatch, ...]) -> tuple[GoalMatch, ...]:
        ids = [value.goal_id for value in values]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate Goal matches are not allowed")
        return tuple(sorted(values, key=lambda value: value.goal_id))


class EventContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1, max_length=100)
    event_type: str = Field(min_length=1, max_length=100)
    occurred_at: datetime
    sender_name: str = Field(max_length=500)
    sender_address: str = Field(max_length=500)
    subject: str = Field(max_length=1000)
    snippet: str = Field(max_length=2000)
    label_ids: tuple[str, ...]
    plain_text: str

    _normalize_required = field_validator("source", "event_type", mode="before")(_required_text)
    _normalize_optional = field_validator(
        "sender_name", "sender_address", "subject", "snippet", "plain_text", mode="before"
    )(_trim_text)
    _require_aware = field_validator("occurred_at")(_aware)

    @field_validator("label_ids")
    @classmethod
    def normalize_labels(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({value.strip() for value in values if value.strip()}))


class GoalContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    domain: str = Field(min_length=1, max_length=100)
    priority: int = Field(ge=0, le=100)

    _normalize_text = field_validator("title", "summary", "domain", mode="before")(_required_text)


class SituationContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(max_length=2000)
    current_state: str = Field(min_length=1, max_length=100)
    attention: AttentionLevel
    last_activity_at: datetime

    _normalize_required = field_validator("title", "current_state", mode="before")(_required_text)
    _normalize_summary = field_validator("summary", mode="before")(_trim_text)
    _require_aware = field_validator("last_activity_at")(_aware)


class EvaluationContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    event: EventContext
    goals: tuple[GoalContext, ...]
    situations: tuple[SituationContext, ...]


class ReevaluateEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    evaluation_key: UUID
    reason: str = Field(min_length=1, max_length=500)
    requested_at: datetime

    _normalize_reason = field_validator("reason", mode="before")(_required_text)
    _require_aware = field_validator("requested_at")(_aware)


class AIRelevancePayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal[RelevanceMethod.AI] = RelevanceMethod.AI
    result: ClassifierResult
    disposition: RelevanceDisposition


class DeterministicRelevancePayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal[RelevanceMethod.DETERMINISTIC] = RelevanceMethod.DETERMINISTIC
    reason: ScreeningReason
    confidence: float = Field(default=1.0, ge=1.0, le=1.0)
    disposition: Literal[RelevanceDisposition.IGNORE] = RelevanceDisposition.IGNORE


RelevanceSignalPayload = Annotated[
    AIRelevancePayload | DeterministicRelevancePayload,
    Field(discriminator="method"),
]


class SignalDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid7)
    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    payload: RelevanceSignalPayload
    confidence: float = Field(ge=0.0, le=1.0)
    disposition: RelevanceDisposition
    producer: SignalProducer
    provider: str = Field(min_length=1, max_length=50)
    model: str | None = Field(default=None, max_length=100)
    classifier_version: str = Field(min_length=1, max_length=100)
    policy_version: str = Field(min_length=1, max_length=100)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    trigger: EvaluationTrigger
    operator_reason: str | None = Field(default=None, max_length=500)
    evaluation_key: UUID
    successful_attempt_id: UUID | None = None
    goal_matches: tuple[GoalMatch, ...] = ()
    created_at: datetime

    _normalize_required = field_validator(
        "provider", "classifier_version", "policy_version", mode="before"
    )(_required_text)
    _normalize_model = field_validator("model", mode="before")(_optional_text)
    _normalize_reason = field_validator("operator_reason", mode="before")(_optional_text)
    _require_aware = field_validator("created_at")(_aware)

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        explicit = self.trigger is EvaluationTrigger.EXPLICIT_REEVALUATION
        if explicit != (self.operator_reason is not None):
            raise ValueError("operator reason is required exactly for explicit re-evaluation")
        if self.payload.disposition is not self.disposition:
            raise ValueError("payload disposition must match Signal disposition")
        if self.producer is SignalProducer.AI:
            if not isinstance(self.payload, AIRelevancePayload):
                raise ValueError("payload must match producer")
            if self.model is None or self.successful_attempt_id is None:
                raise ValueError("AI Signals require model and successful attempt")
            if self.goal_matches != self.payload.result.goal_matches:
                raise ValueError("Signal Goal matches must equal classifier Goal matches")
        else:
            if not isinstance(self.payload, DeterministicRelevancePayload):
                raise ValueError("payload must match producer")
            if (
                self.model is not None
                or self.successful_attempt_id is not None
                or self.goal_matches
            ):
                raise ValueError("deterministic Signals cannot contain AI provenance")
            if self.confidence != 1.0:
                raise ValueError("deterministic Signals require confidence 1")
        return self


class SignalRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    kind: SignalKind
    schema_version: int = Field(ge=1)
    payload: RelevanceSignalPayload
    confidence: float = Field(ge=0.0, le=1.0)
    disposition: RelevanceDisposition
    producer: SignalProducer
    provider: str
    model: str | None
    classifier_version: str
    policy_version: str
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    trigger: EvaluationTrigger
    operator_reason: str | None
    evaluation_key: UUID
    successful_attempt_id: UUID | None
    situation_id: UUID | None
    supersedes_signal_id: UUID | None
    is_current: bool
    goal_matches: tuple[GoalMatch, ...]
    created_at: datetime

    _normalize_required = field_validator(
        "provider", "classifier_version", "policy_version", mode="before"
    )(_required_text)
    _normalize_model = field_validator("model", mode="before")(_optional_text)
    _normalize_reason = field_validator("operator_reason", mode="before")(_optional_text)
    _require_aware = field_validator("created_at")(_aware)


class StartEvaluationAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid7)
    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    evaluation_key: UUID
    attempt_number: int = Field(ge=1)
    trigger: EvaluationTrigger
    operator_reason: str | None = Field(default=None, max_length=500)
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=100)
    classifier_version: str = Field(min_length=1, max_length=100)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: datetime

    _normalize_required = field_validator("provider", "model", "classifier_version", mode="before")(
        _required_text
    )
    _normalize_reason = field_validator("operator_reason", mode="before")(_optional_text)
    _require_aware = field_validator("started_at")(_aware)

    @model_validator(mode="after")
    def require_explicit_reason(self) -> Self:
        explicit = self.trigger is EvaluationTrigger.EXPLICIT_REEVALUATION
        if explicit != (self.operator_reason is not None):
            raise ValueError("operator reason is required exactly for explicit re-evaluation")
        return self


class FinishEvaluationAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: UUID
    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    evaluation_key: UUID
    status: Literal[
        EvaluationAttemptStatus.SUCCEEDED,
        EvaluationAttemptStatus.RETRYABLE_FAILURE,
        EvaluationAttemptStatus.PERMANENT_FAILURE,
    ]
    failure_code: str | None = Field(default=None, max_length=100)
    completed_at: datetime

    _normalize_failure = field_validator("failure_code", mode="before")(_optional_text)
    _require_aware = field_validator("completed_at")(_aware)

    @model_validator(mode="after")
    def require_failure_code(self) -> Self:
        succeeded = self.status is EvaluationAttemptStatus.SUCCEEDED
        if succeeded == (self.failure_code is not None):
            raise ValueError("failure code is required exactly for failed attempts")
        return self


class EvaluationAttemptRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    evaluation_key: UUID
    attempt_number: int = Field(ge=1)
    trigger: EvaluationTrigger
    operator_reason: str | None
    status: EvaluationAttemptStatus
    provider: str
    model: str
    classifier_version: str
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_code: str | None
    started_at: datetime
    completed_at: datetime | None

    _require_aware = field_validator("started_at")(_aware)

    @field_validator("completed_at")
    @classmethod
    def require_completed_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)
