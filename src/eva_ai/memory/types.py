import json
import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid7

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from eva_ai.situations.types import AttentionLevel

_SLOT_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class MemorySourceType(StrEnum):
    USER_EXPLICIT = "USER_EXPLICIT"
    USER_BEHAVIOR = "USER_BEHAVIOR"
    AGENT_INFERRED = "AGENT_INFERRED"
    EXTERNAL_EVENT = "EXTERNAL_EVENT"
    SYSTEM_OBSERVED = "SYSTEM_OBSERVED"


class MemoryScopeType(StrEnum):
    WORKSPACE = "WORKSPACE"
    GOAL = "GOAL"
    SITUATION = "SITUATION"


class MemoryFactStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    RETRACTED = "RETRACTED"


class MemoryEpisodeType(StrEnum):
    DECISION = "DECISION"
    EXPERIENCE = "EXPERIENCE"
    OUTCOME = "OUTCOME"
    SITUATION_SUMMARY = "SITUATION_SUMMARY"


class EpisodicMemoryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RETRACTED = "RETRACTED"


class MemoryProposalKind(StrEnum):
    FACT = "FACT"
    EPISODE = "EPISODE"


def _required_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


def _slot_component(value: object) -> object:
    normalized = _required_text(value)
    if not isinstance(normalized, str):
        return normalized
    normalized = normalized.casefold()
    if not _SLOT_COMPONENT.fullmatch(normalized):
        raise ValueError("must use lowercase letters, numbers, dots, underscores, or hyphens")
    return normalized


def _aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


def _bounded_json(value: JsonValue) -> JsonValue:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 8192:
        raise ValueError("value_json must serialize to at most 8 KiB")
    return value


def _entities(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = {value.strip().casefold() for value in values if value.strip()}
    if any(len(value) > 200 for value in normalized):
        raise ValueError("entities must be at most 200 characters")
    return tuple(sorted(normalized))


def _goal_ids(values: tuple[UUID, ...]) -> tuple[UUID, ...]:
    return tuple(sorted(set(values)))


class MemoryFactDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid7)
    user_id: UUID
    workspace_id: UUID
    namespace: str = Field(max_length=100)
    key: str = Field(max_length=200)
    value_json: JsonValue
    scope_type: MemoryScopeType
    scope_id: UUID
    source_type: MemorySourceType
    source_ref: str = Field(max_length=500)
    confidence: Decimal = Field(ge=0, le=1)
    idempotency_key: str = Field(max_length=500)
    valid_from: datetime
    valid_until: datetime | None = None

    _normalize_slot = field_validator("namespace", "key", mode="before")(_slot_component)
    _normalize_required = field_validator("source_ref", "idempotency_key", mode="before")(
        _required_text
    )
    _limit_value = field_validator("value_json")(_bounded_json)
    _require_aware = field_validator("valid_from", "valid_until")(
        lambda value: None if value is None else _aware(value)
    )

    @model_validator(mode="after")
    def validate_scope_and_window(self) -> Self:
        if self.scope_type is MemoryScopeType.WORKSPACE and self.scope_id != self.workspace_id:
            raise ValueError("Workspace scope must use the Workspace ID")
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        return self


class MemoryFactRecord(MemoryFactDraft):
    status: MemoryFactStatus
    supersedes_memory_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    _require_record_aware = field_validator("created_at", "updated_at")(_aware)


class EpisodicMemoryDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid7)
    user_id: UUID
    workspace_id: UUID
    type: MemoryEpisodeType
    summary: str = Field(max_length=4000)
    entities: tuple[str, ...] = Field(default=(), max_length=20)
    goal_ids: tuple[UUID, ...] = Field(default=(), max_length=20)
    situation_id: UUID | None = None
    importance: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    source_type: MemorySourceType
    source_ref: str = Field(max_length=500)
    idempotency_key: str = Field(max_length=500)
    occurred_at: datetime

    _normalize_summary = field_validator("summary", mode="before")(_required_text)
    _normalize_required = field_validator("source_ref", "idempotency_key", mode="before")(
        _required_text
    )
    _normalize_entities = field_validator("entities")(_entities)
    _normalize_goals = field_validator("goal_ids")(_goal_ids)
    _require_aware = field_validator("occurred_at")(_aware)


class EpisodicMemoryRecord(EpisodicMemoryDraft):
    status: EpisodicMemoryStatus
    embedding_model: str = Field(max_length=100)
    embedding_dimensions: int = Field(ge=1)
    embedding_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedded_at: datetime
    created_at: datetime

    _normalize_model = field_validator("embedding_model", mode="before")(_required_text)
    _require_record_aware = field_validator("embedded_at", "created_at")(_aware)


class MemoryProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: MemoryProposalKind
    claim: str = Field(max_length=4000)
    namespace: str | None = Field(default=None, max_length=100)
    key: str | None = Field(default=None, max_length=200)
    scope_type: MemoryScopeType | None = None
    scope_id: UUID | None = None
    source_type: MemorySourceType
    source_ref: str = Field(max_length=500)
    confidence: Decimal = Field(ge=0, le=1)
    reason: str = Field(max_length=1000)

    _normalize_text = field_validator("claim", "source_ref", "reason", mode="before")(
        _required_text
    )
    _normalize_optional_slot = field_validator("namespace", "key", mode="before")(
        lambda value: None if value is None else _slot_component(value)
    )

    @model_validator(mode="after")
    def validate_fact_shape(self) -> Self:
        fact_fields = (self.namespace, self.key, self.scope_type, self.scope_id)
        if self.kind is MemoryProposalKind.FACT and any(value is None for value in fact_fields):
            raise ValueError("Fact proposals require namespace, key, and scope")
        if self.kind is MemoryProposalKind.EPISODE and any(
            value is not None for value in fact_fields
        ):
            raise ValueError("Episode proposals cannot contain fact slot fields")
        return self


class ContextSituation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    title: str
    summary: str
    current_state: str
    next_action: str | None
    next_expected: str | None
    attention: AttentionLevel
    last_activity_at: datetime

    _require_aware = field_validator("last_activity_at")(_aware)


class ContextGoal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    title: str
    objective: str
    domain: str
    priority: int = Field(ge=0, le=100)


class RankedEpisode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    memory: EpisodicMemoryRecord
    score: float = Field(ge=0, le=1)
    semantic_similarity: float = Field(ge=0, le=1)


class SemanticCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    memory: EpisodicMemoryRecord
    distance: float = Field(ge=0)


class AgentWorkingContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    user_id: UUID
    workspace_id: UUID
    situation: ContextSituation
    goals: tuple[ContextGoal, ...]
    facts: tuple[MemoryFactRecord, ...]
    episodes: tuple[RankedEpisode, ...]
    built_at: datetime
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    _require_aware = field_validator("built_at")(_aware)
