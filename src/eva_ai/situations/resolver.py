from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

from eva_ai.situations.errors import SituationResolutionError
from eva_ai.situations.types import (
    AttentionLevel,
    ResolveEvent,
    SituationLifecycle,
    SituationResolution,
    SituationType,
)

_GMAIL_SOURCE = "gmail"
_RECEIVED_EMAIL_TYPE = "email.received"
_GMAIL_THREAD_PREFIX = "gmail-thread:"


class ResolvableEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: UUID
    workspace_id: UUID
    source: str
    event_type: str
    occurred_at: datetime
    payload: dict[str, JsonValue]
    correlation_keys: tuple[str, ...]


class InitialSituationSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: SituationType
    title: str
    lifecycle: SituationLifecycle
    attention: AttentionLevel
    summary: str
    current_state: str
    next_action: str | None
    next_expected: str | None
    last_activity_at: datetime


class SituationResolutionRepository(Protocol):
    async def get_event_for_resolution(
        self,
        *,
        event_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
    ) -> ResolvableEvent | None: ...

    async def resolve_gmail_event(
        self,
        *,
        command: ResolveEvent,
        correlation_key: str,
        initial_snapshot: InitialSituationSnapshot,
    ) -> SituationResolution: ...


class SituationResolver:
    def __init__(self, repository: SituationResolutionRepository) -> None:
        self._repository = repository

    async def resolve(self, command: ResolveEvent) -> SituationResolution:
        # Resolution is intentionally opt-in. Milestone 4 relevance routing will decide which
        # Events reach this service; Gmail ingestion itself remains provider-only.
        event = await self._repository.get_event_for_resolution(
            event_id=command.event_id,
            user_id=command.user_id,
            workspace_id=command.workspace_id,
        )
        if event is None or (
            event.id != command.event_id
            or event.user_id != command.user_id
            or event.workspace_id != command.workspace_id
        ):
            raise SituationResolutionError
        if event.source != _GMAIL_SOURCE or event.event_type != _RECEIVED_EMAIL_TYPE:
            raise SituationResolutionError
        supported_keys = tuple(
            key
            for key in event.correlation_keys
            if key.startswith(_GMAIL_THREAD_PREFIX)
            and key.removeprefix(_GMAIL_THREAD_PREFIX).strip()
        )
        if len(supported_keys) != 1:
            raise SituationResolutionError

        return await self._repository.resolve_gmail_event(
            command=command,
            correlation_key=supported_keys[0],
            initial_snapshot=_initial_snapshot(event),
        )


def _initial_snapshot(event: ResolvableEvent) -> InitialSituationSnapshot:
    headers = event.payload.get("headers")
    raw_subject = headers.get("subject") if isinstance(headers, dict) else None
    title = _bounded_text(raw_subject, 300) or "Gmail conversation"
    summary = _bounded_text(event.payload.get("snippet"), 2000)
    return InitialSituationSnapshot(
        type=SituationType.EMAIL_THREAD,
        title=title,
        lifecycle=SituationLifecycle.OPEN,
        attention=AttentionLevel.NORMAL,
        summary=summary,
        current_state="NEW",
        next_action=None,
        next_expected=None,
        last_activity_at=event.occurred_at,
    )


def _bounded_text(value: JsonValue | None, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]
