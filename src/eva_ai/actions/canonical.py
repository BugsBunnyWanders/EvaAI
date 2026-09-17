import hashlib
import json
import re
from email.utils import parseaddr
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_EMAIL_PATTERN = re.compile(r"^[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+$")


def _normalize_address(value: str) -> str:
    if "," in value:
        raise ValueError("recipient must be one valid email address")
    display_name, address = parseaddr(value.strip())
    del display_name
    normalized = address.strip().lower()
    if not normalized or _EMAIL_PATTERN.fullmatch(normalized) is None:
        raise ValueError("recipient must be one valid email address")
    return normalized


def _normalize_addresses(values: object) -> tuple[str, ...]:
    if not isinstance(values, list | tuple):
        raise ValueError("recipients must be a sequence of valid email addresses")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("recipient must be one valid email address")
        normalized = _normalize_address(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _required_single_line(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


def _required_body(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


class CanonicalEmail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["NEW", "REPLY"]
    to: tuple[str, ...]
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    subject: str = Field(max_length=998)
    text_body: str = Field(max_length=100_000)
    html_body: str | None = Field(default=None, max_length=200_000)
    thread_id: str | None = Field(default=None, max_length=500)
    in_reply_to: str | None = Field(default=None, max_length=998)
    references: tuple[str, ...] = Field(default=(), max_length=100)
    attachments: tuple[str, ...] = ()

    _normalize_recipients = field_validator("to", "cc", "bcc", mode="before")(
        _normalize_addresses
    )
    _normalize_subject = field_validator("subject", mode="before")(_required_single_line)
    _normalize_text = field_validator("text_body", mode="before")(_required_body)

    @field_validator("html_body", mode="before")
    @classmethod
    def normalize_html(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("thread_id", "in_reply_to", mode="before")
    @classmethod
    def normalize_optional_identifier(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("references", mode="before")
    @classmethod
    def normalize_references(cls, value: object) -> object:
        if not isinstance(value, list | tuple):
            return value
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())

    @field_validator("attachments")
    @classmethod
    def reject_attachments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value:
            raise ValueError("attachments are not supported")
        return value

    @model_validator(mode="after")
    def validate_delivery_context(self) -> Self:
        if not (self.to or self.cc or self.bcc):
            raise ValueError("at least one recipient is required")
        has_reply_context = any((self.thread_id, self.in_reply_to, self.references))
        if self.mode == "REPLY" and not (self.thread_id and self.in_reply_to):
            raise ValueError("reply context requires thread_id and in_reply_to")
        if self.mode == "NEW" and has_reply_context:
            raise ValueError("new email cannot include reply context")
        return self


def canonical_email_hash(message: CanonicalEmail) -> str:
    """Hash the exact normalized message that an approval authorizes Gmail to send."""
    payload = json.dumps(
        message.model_dump(mode="json", exclude_none=False),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
