from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _required_text(value: object) -> object:
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized
    return value


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


class TelegramAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class TelegramAccountRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    user_id: UUID
    workspace_id: UUID
    telegram_user_id: int = Field(gt=0)
    chat_id: int
    username: str | None = Field(default=None, max_length=100)
    first_name: str | None = Field(default=None, max_length=200)
    status: TelegramAccountStatus
    paired_at: datetime
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    _require_aware = field_validator("paired_at", "revoked_at", "created_at", "updated_at")(_aware)


class PairingCodeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    user_id: UUID
    workspace_id: UUID
    expires_at: datetime
    consumed_at: datetime | None
    telegram_account_id: UUID | None
    created_at: datetime

    _require_aware = field_validator("expires_at", "consumed_at", "created_at")(_aware)


class PairingLink(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code_id: UUID
    token: str = Field(min_length=32, max_length=200, repr=False)
    url: str = Field(max_length=500)
    expires_at: datetime

    _require_aware = field_validator("expires_at")(_aware)


class TelegramUser(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int = Field(gt=0)
    is_bot: bool = False
    first_name: str = Field(max_length=200)
    username: str | None = Field(default=None, max_length=100)


class TelegramChat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int
    type: str = Field(max_length=32)


class TelegramReplyMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    message_id: int = Field(gt=0)


class TelegramMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    message_id: int = Field(gt=0)
    date: int = Field(ge=0)
    chat: TelegramChat
    from_user: TelegramUser | None = Field(default=None, alias="from")
    # Keep inbound text within the same durable conversation/agent bound used for replies.
    text: str | None = Field(default=None, max_length=4000)
    reply_to_message: TelegramReplyMessage | None = None


class TelegramCallbackQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1, max_length=200)
    from_user: TelegramUser = Field(alias="from")
    message: TelegramMessage | None = None
    data: str | None = Field(default=None, max_length=128)


class TelegramUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    update_id: int = Field(ge=0)
    message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None

    @model_validator(mode="after")
    def require_supported_shape(self) -> TelegramUpdate:
        if self.message is None and self.callback_query is None:
            raise ValueError("update has no supported payload")
        return self


class TelegramSendResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: int = Field(gt=0)
    chat_id: int


class TelegramWebhookInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    pending_update_count: int = Field(ge=0)
    last_error_date: int | None = None
    last_error_message: str | None = Field(default=None, max_length=500)


class TelegramTurnRequestedMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_type: Literal["telegram.turn.requested"] = "telegram.turn.requested"
    outbox_message_id: UUID
    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    schema_version: int = Field(default=1, gt=0)


class PairingConsumeCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str = Field(min_length=32, max_length=200, repr=False)
    telegram_user_id: int = Field(gt=0)
    chat_id: int
    username: str | None = Field(default=None, max_length=100)
    first_name: str | None = Field(default=None, max_length=200)
    consumed_at: datetime

    _token = field_validator("token", mode="before")(_required_text)
    _require_aware = field_validator("consumed_at")(_aware)


class WebhookDisposition(StrEnum):
    INGESTED = "INGESTED"
    DUPLICATE = "DUPLICATE"
    PAIRED = "PAIRED"
    IGNORED = "IGNORED"


class WebhookResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    disposition: WebhookDisposition
    event_id: UUID | None = None
