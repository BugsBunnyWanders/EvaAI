import re
from email.utils import parseaddr
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_EMAIL_PATTERN = re.compile(r"^[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+$")


class RecipientResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"


class RecipientCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: str = Field(default="", max_length=500)
    email_address: str = Field(max_length=500)

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("email_address", mode="before")
    @classmethod
    def normalize_address(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = _email_address(value)
        if normalized is None:
            raise ValueError("candidate email address is invalid")
        return normalized


class RecipientResolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: RecipientResolutionStatus
    email_address: str | None = None
    clarification: str | None = None


def resolve_recipient(
    value: str,
    candidates: tuple[RecipientCandidate, ...],
) -> RecipientResolution:
    normalized_value = " ".join(value.split())
    explicit = _email_address(normalized_value)
    if explicit is not None:
        return RecipientResolution(
            status=RecipientResolutionStatus.RESOLVED,
            email_address=explicit,
        )

    query = normalized_value.casefold()
    matches: dict[str, RecipientCandidate] = {}
    for candidate in candidates:
        name = candidate.display_name.casefold()
        if query and (query == name or query in name):
            matches.setdefault(candidate.email_address, candidate)

    if len(matches) == 1:
        return RecipientResolution(
            status=RecipientResolutionStatus.RESOLVED,
            email_address=next(iter(matches)),
        )
    if len(matches) > 1:
        clarification = (
            f"I found multiple email addresses for {normalized_value}. Which address should I use?"
        )
    else:
        clarification = f"What email address should I use for {normalized_value}?"
    return RecipientResolution(
        status=RecipientResolutionStatus.CLARIFICATION_REQUIRED,
        clarification=clarification,
    )


def _email_address(value: str) -> str | None:
    if "," in value:
        return None
    _, address = parseaddr(value)
    normalized = address.strip().lower()
    return normalized if _EMAIL_PATTERN.fullmatch(normalized) is not None else None
