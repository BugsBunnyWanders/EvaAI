import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

JsonObject = dict[str, JsonValue]
SAFE_AUTONOMY_POLICY: JsonObject = {"mode": "REQUIRE_APPROVAL"}


class GoalMode(StrEnum):
    ACHIEVE = "ACHIEVE"
    MAINTAIN = "MAINTAIN"


class GoalStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


class GoalSource(StrEnum):
    USER_EXPLICIT = "USER_EXPLICIT"
    AGENT_INFERRED = "AGENT_INFERRED"


def _normalize_required_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


def _normalize_criteria(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(value.strip() for value in values)
    if any(not value for value in normalized):
        raise ValueError("success criteria must not be blank")
    if any(len(value) > 500 for value in normalized):
        raise ValueError("success criteria must be at most 500 characters")
    return normalized


def _validate_constraints(value: JsonObject) -> JsonObject:
    serialized = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(serialized) > 8192:
        raise ValueError("constraints must serialize to at most 8 KiB")
    return value


def _validate_aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


class GoalDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    workspace_id: UUID
    title: str = Field(max_length=200)
    objective: str = Field(max_length=4000)
    domain: str = Field(max_length=100)
    mode: GoalMode
    priority: int = Field(default=50, ge=0, le=100)
    success_criteria: tuple[str, ...] = Field(default=(), max_length=20)
    constraints: JsonObject = Field(default_factory=dict)
    parent_goal_id: UUID | None = None

    _normalize_text = field_validator("title", "objective", "domain", mode="before")(
        _normalize_required_text
    )
    _normalize_success_criteria = field_validator("success_criteria")(_normalize_criteria)
    _limit_constraints = field_validator("constraints")(_validate_constraints)


class InferredGoalDraft(GoalDraft):
    confidence: Decimal = Field(ge=0, le=1)


class GoalUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    workspace_id: UUID
    goal_id: UUID
    title: str | None = Field(default=None, max_length=200)
    objective: str | None = Field(default=None, max_length=4000)
    domain: str | None = Field(default=None, max_length=100)
    mode: GoalMode | None = None
    priority: int | None = Field(default=None, ge=0, le=100)
    success_criteria: tuple[str, ...] | None = Field(default=None, max_length=20)
    constraints: JsonObject | None = None
    parent_goal_id: UUID | None = None
    clear_parent: bool = False
    status: GoalStatus | None = None

    _normalize_text = field_validator("title", "objective", "domain", mode="before")(
        lambda value: value if value is None else _normalize_required_text(value)
    )

    @field_validator("success_criteria")
    @classmethod
    def normalize_success_criteria(cls, values: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return None if values is None else _normalize_criteria(values)

    @field_validator("constraints")
    @classmethod
    def limit_constraints(cls, value: JsonObject | None) -> JsonObject | None:
        return None if value is None else _validate_constraints(value)

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if self.parent_goal_id is not None and self.clear_parent:
            raise ValueError("parent_goal_id and clear_parent are mutually exclusive")
        changed = any(
            getattr(self, field_name) is not None
            for field_name in (
                "title",
                "objective",
                "domain",
                "mode",
                "priority",
                "success_criteria",
                "constraints",
                "parent_goal_id",
                "status",
            )
        )
        if not changed and not self.clear_parent:
            raise ValueError("at least one Goal change is required")
        return self


class GoalRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: UUID
    workspace_id: UUID
    title: str = Field(max_length=200)
    objective: str = Field(max_length=4000)
    domain: str = Field(max_length=100)
    mode: GoalMode
    priority: int = Field(ge=0, le=100)
    status: GoalStatus
    success_criteria: tuple[str, ...] = Field(max_length=20)
    constraints: JsonObject
    autonomy_policy: JsonObject = Field(default_factory=lambda: dict(SAFE_AUTONOMY_POLICY))
    source: GoalSource
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    parent_goal_id: UUID | None
    created_at: datetime
    updated_at: datetime

    _normalize_text = field_validator("title", "objective", "domain", mode="before")(
        _normalize_required_text
    )
    _normalize_success_criteria = field_validator("success_criteria")(_normalize_criteria)
    _limit_constraints = field_validator("constraints")(_validate_constraints)
    _require_aware = field_validator("created_at", "updated_at")(_validate_aware)

    @field_validator("autonomy_policy")
    @classmethod
    def require_safe_autonomy_policy(cls, value: JsonObject) -> JsonObject:
        if value != SAFE_AUTONOMY_POLICY:
            raise ValueError("autonomy policy must require approval")
        return value
