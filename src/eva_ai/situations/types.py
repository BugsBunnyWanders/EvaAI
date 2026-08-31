from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SituationType(StrEnum):
    EMAIL_THREAD = "EMAIL_THREAD"


class SituationLifecycle(StrEnum):
    OPEN = "OPEN"
    ACTIVE = "ACTIVE"
    WAITING_USER = "WAITING_USER"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    RESOLVED = "RESOLVED"
    ABANDONED = "ABANDONED"


class AttentionLevel(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class CorrelationMethod(StrEnum):
    DETERMINISTIC_KEY = "DETERMINISTIC_KEY"
    EXPLICIT = "EXPLICIT"


class CorrelationKeyKind(StrEnum):
    GMAIL_THREAD = "GMAIL_THREAD"


class GoalContribution(StrEnum):
    SUPPORTS = "SUPPORTS"
    BLOCKS = "BLOCKS"
    CONTEXT = "CONTEXT"


def _normalize_required_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


def _normalize_optional_text(value: object) -> object:
    if value is None:
        return None
    return _normalize_required_text(value)


def _normalize_summary(value: object) -> object:
    if isinstance(value, str):
        return value.strip()
    return value


def _validate_aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


def _sorted_unique(values: tuple[UUID, ...]) -> tuple[UUID, ...]:
    return tuple(sorted(set(values)))


class SituationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: UUID
    workspace_id: UUID
    type: SituationType
    title: str = Field(max_length=300)
    lifecycle: SituationLifecycle
    attention: AttentionLevel
    summary: str = Field(max_length=2000)
    current_state: str = Field(max_length=100)
    next_action: str | None = Field(default=None, max_length=1000)
    next_expected: str | None = Field(default=None, max_length=1000)
    version: int = Field(ge=1)
    last_activity_at: datetime
    created_at: datetime
    updated_at: datetime

    _normalize_required = field_validator("title", "current_state", mode="before")(
        _normalize_required_text
    )
    _normalize_summary = field_validator("summary", mode="before")(_normalize_summary)
    _normalize_optional = field_validator("next_action", "next_expected", mode="before")(
        _normalize_optional_text
    )
    _require_aware = field_validator("last_activity_at", "created_at", "updated_at")(
        _validate_aware
    )


class SituationSnapshotUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    workspace_id: UUID
    situation_id: UUID
    expected_version: int = Field(ge=1)
    title: str = Field(max_length=300)
    summary: str = Field(max_length=2000)
    current_state: str = Field(max_length=100)
    next_action: str | None = Field(default=None, max_length=1000)
    next_expected: str | None = Field(default=None, max_length=1000)
    updated_at: datetime

    _normalize_required = field_validator("title", "current_state", mode="before")(
        _normalize_required_text
    )
    _normalize_summary = field_validator("summary", mode="before")(_normalize_summary)
    _normalize_optional = field_validator("next_action", "next_expected", mode="before")(
        _normalize_optional_text
    )
    _require_aware = field_validator("updated_at")(_validate_aware)


class LinkSituationGoal(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    workspace_id: UUID
    situation_id: UUID
    goal_id: UUID
    relevance: Decimal = Field(ge=0, le=1)
    contribution: GoalContribution
    reasoning: str | None = Field(default=None, max_length=1000)
    linked_at: datetime

    _normalize_reasoning = field_validator("reasoning", mode="before")(_normalize_optional_text)
    _require_aware = field_validator("linked_at")(_validate_aware)


class ResolveEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    goal_ids: tuple[UUID, ...] = ()
    resolved_at: datetime

    _normalize_goal_ids = field_validator("goal_ids")(_sorted_unique)
    _require_aware = field_validator("resolved_at")(_validate_aware)


class SituationEventRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    situation_id: UUID
    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    correlation_method: CorrelationMethod
    correlation_key: str | None = Field(default=None, max_length=500)
    event_occurred_at: datetime
    linked_at: datetime

    _normalize_key = field_validator("correlation_key", mode="before")(_normalize_optional_text)
    _require_aware = field_validator("event_occurred_at", "linked_at")(_validate_aware)


class SituationGoalRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    situation_id: UUID
    goal_id: UUID
    user_id: UUID
    workspace_id: UUID
    relevance: Decimal = Field(ge=0, le=1)
    contribution: GoalContribution
    reasoning: str | None = Field(default=None, max_length=1000)
    linked_at: datetime

    _normalize_reasoning = field_validator("reasoning", mode="before")(_normalize_optional_text)
    _require_aware = field_validator("linked_at")(_validate_aware)


class SituationResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    situation: SituationRecord
    situation_created: bool
    event_link_created: bool
    linked_goal_ids: tuple[UUID, ...]

    _normalize_goal_ids = field_validator("linked_goal_ids")(_sorted_unique)
