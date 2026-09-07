from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eva_ai.agent.types import NotificationUrgency


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


class NotificationChannel(StrEnum):
    TELEGRAM = "TELEGRAM"


class NotificationKind(StrEnum):
    PROACTIVE = "PROACTIVE"
    REACTIVE = "REACTIVE"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


class NotificationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    user_id: UUID
    workspace_id: UUID
    event_id: UUID
    situation_id: UUID | None
    agent_run_id: UUID | None
    channel: NotificationChannel
    kind: NotificationKind
    urgency: NotificationUrgency
    message: str = Field(max_length=4000)
    dedupe_key: str = Field(max_length=500)
    status: NotificationStatus
    attempt_count: int = Field(ge=0)
    next_retry_at: datetime | None
    claim_id: UUID | None
    lease_expires_at: datetime | None
    telegram_account_id: UUID | None
    provider_chat_id: int | None
    provider_message_id: int | None
    failure_code: str | None = Field(default=None, max_length=100)
    failure_summary: str | None = Field(default=None, max_length=500)
    created_at: datetime
    sent_at: datetime | None

    _require_aware = field_validator("next_retry_at", "lease_expires_at", "created_at", "sent_at")(
        _aware
    )


class NotificationDeliveryRequestedMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_type: Literal["notification.delivery.requested"] = "notification.delivery.requested"
    outbox_message_id: UUID
    notification_id: UUID
    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    schema_version: int = Field(default=1, gt=0)


class DeliveryOutcome(StrEnum):
    SENT = "SENT"
    TERMINAL = "TERMINAL"
    RETRY = "RETRY"
    DEFERRED = "DEFERRED"
