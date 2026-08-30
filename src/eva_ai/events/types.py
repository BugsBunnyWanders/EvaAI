from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid7

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class PrincipalType(StrEnum):
    USER = "USER"
    SYSTEM = "SYSTEM"
    EXTERNAL = "EXTERNAL"


class ProcessingStage(StrEnum):
    RECEIVED = "RECEIVED"
    NORMALIZED = "NORMALIZED"
    ENRICHED = "ENRICHED"
    CLASSIFIED = "CLASSIFIED"
    CORRELATED = "CORRELATED"
    HANDLED = "HANDLED"


class OutboxState(StrEnum):
    PENDING = "PENDING"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"


class NewEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid7)
    user_id: UUID
    workspace_id: UUID
    source: str
    event_type: str
    external_id: str | None = None
    idempotency_key: str
    occurred_at: datetime
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    principal_type: PrincipalType
    principal_id: UUID | None = None
    actor: dict[str, JsonValue] | None = None
    subject: dict[str, JsonValue] | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    correlation_keys: list[str] = Field(default_factory=list)
    schema_version: int = Field(default=1, gt=0)

    @field_validator("source", "event_type", "idempotency_key")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def require_aware_timestamps(self) -> Self:
        for value in (self.occurred_at, self.received_at):
            if value.utcoffset() is None:
                raise ValueError("timestamps must include a timezone")
        return self


class EventAvailableMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    outbox_message_id: UUID
    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    event_type: str
    schema_version: int = Field(gt=0)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    outbox_message_id: UUID
    destination: str
    envelope: EventAvailableMessage
