from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid7

import pytest

from eva_ai.situations import (
    AttentionLevel,
    ResolveEvent,
    SituationLifecycle,
    SituationRecord,
    SituationResolution,
    SituationResolutionError,
    SituationType,
)
from eva_ai.situations.resolver import (
    InitialSituationSnapshot,
    ResolvableEvent,
    SituationResolver,
)

OCCURRED_AT = datetime(2026, 9, 1, 7, 0, tzinfo=UTC)
RESOLVED_AT = OCCURRED_AT + timedelta(hours=2)


class FakeResolutionRepository:
    def __init__(self, event: ResolvableEvent | None) -> None:
        self.event = event
        self.received_command: ResolveEvent | None = None
        self.received_key: str | None = None
        self.received_snapshot: InitialSituationSnapshot | None = None

    async def get_event_for_resolution(
        self,
        *,
        event_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
    ) -> ResolvableEvent | None:
        del event_id, user_id, workspace_id
        return self.event

    async def resolve_gmail_event(
        self,
        *,
        command: ResolveEvent,
        correlation_key: str,
        initial_snapshot: InitialSituationSnapshot,
    ) -> SituationResolution:
        self.received_command = command
        self.received_key = correlation_key
        self.received_snapshot = initial_snapshot
        return SituationResolution(
            situation=SituationRecord(
                id=uuid7(),
                user_id=command.user_id,
                workspace_id=command.workspace_id,
                type=initial_snapshot.type,
                title=initial_snapshot.title,
                lifecycle=initial_snapshot.lifecycle,
                attention=initial_snapshot.attention,
                summary=initial_snapshot.summary,
                current_state=initial_snapshot.current_state,
                next_action=initial_snapshot.next_action,
                next_expected=initial_snapshot.next_expected,
                version=1,
                last_activity_at=initial_snapshot.last_activity_at,
                created_at=command.resolved_at,
                updated_at=command.resolved_at,
            ),
            situation_created=True,
            event_link_created=True,
            linked_goal_ids=command.goal_ids,
        )


def gmail_event(
    *,
    source: str = "gmail",
    event_type: str = "email.received",
    correlation_keys: tuple[str, ...] = ("gmail-thread:thread-1",),
    payload: dict[str, object] | None = None,
) -> ResolvableEvent:
    return ResolvableEvent(
        id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        source=source,
        event_type=event_type,
        occurred_at=OCCURRED_AT,
        payload=cast(
            dict[str, object],
            payload
            or {
                "headers": {"subject": "  Re:   Visa   appointment  "},
                "snippet": "  The embassy   has replied.  ",
            },
        ),
        correlation_keys=correlation_keys,
    )


@pytest.mark.asyncio
async def test_resolver_derives_bounded_deterministic_gmail_snapshot() -> None:
    event = gmail_event()
    repository = FakeResolutionRepository(event)
    command = ResolveEvent(
        event_id=event.id,
        user_id=event.user_id,
        workspace_id=event.workspace_id,
        resolved_at=RESOLVED_AT,
    )

    result = await SituationResolver(repository).resolve(command)

    assert repository.received_key == "gmail-thread:thread-1"
    assert repository.received_snapshot == InitialSituationSnapshot(
        type=SituationType.EMAIL_THREAD,
        title="Re: Visa appointment",
        lifecycle=SituationLifecycle.OPEN,
        attention=AttentionLevel.NORMAL,
        summary="The embassy has replied.",
        current_state="NEW",
        next_action=None,
        next_expected=None,
        last_activity_at=OCCURRED_AT,
    )
    assert result.situation.last_activity_at == OCCURRED_AT
    assert result.situation.last_activity_at != RESOLVED_AT


@pytest.mark.asyncio
async def test_resolver_bounds_provider_text_and_uses_safe_fallbacks() -> None:
    event = gmail_event(payload={"headers": {"subject": "x" * 301}, "snippet": " y " * 2001})
    repository = FakeResolutionRepository(event)

    await SituationResolver(repository).resolve(
        ResolveEvent(
            event_id=event.id,
            user_id=event.user_id,
            workspace_id=event.workspace_id,
            resolved_at=RESOLVED_AT,
        )
    )

    assert repository.received_snapshot is not None
    assert repository.received_snapshot.title == "x" * 300
    assert len(repository.received_snapshot.summary) == 2000

    malformed = gmail_event(payload={"headers": [], "snippet": {"not": "text"}})
    malformed_repository = FakeResolutionRepository(malformed)
    await SituationResolver(malformed_repository).resolve(
        ResolveEvent(
            event_id=malformed.id,
            user_id=malformed.user_id,
            workspace_id=malformed.workspace_id,
            resolved_at=RESOLVED_AT,
        )
    )
    assert malformed_repository.received_snapshot is not None
    assert malformed_repository.received_snapshot.title == "Gmail conversation"
    assert malformed_repository.received_snapshot.summary == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        None,
        gmail_event(source="calendar"),
        gmail_event(event_type="email.sent"),
        gmail_event(correlation_keys=()),
        gmail_event(correlation_keys=("entity:person",)),
        gmail_event(correlation_keys=("gmail-thread:",)),
        gmail_event(correlation_keys=("gmail-thread:thread-1", "gmail-thread:thread-1")),
        gmail_event(correlation_keys=("gmail-thread:thread-1", "gmail-thread:thread-2")),
    ],
)
async def test_resolver_rejects_unsupported_or_ambiguous_event_without_writing(
    event: ResolvableEvent | None,
) -> None:
    repository = FakeResolutionRepository(event)
    command = ResolveEvent(
        event_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        resolved_at=RESOLVED_AT,
    )

    with pytest.raises(
        SituationResolutionError,
        match="Event cannot be resolved into a Situation",
    ):
        await SituationResolver(repository).resolve(command)

    assert repository.received_command is None


@pytest.mark.asyncio
async def test_resolver_passes_normalized_goal_ids_to_atomic_repository_call() -> None:
    event = gmail_event()
    repository = FakeResolutionRepository(event)
    lower = UUID("00000000-0000-7000-8000-000000000001")
    higher = UUID("ffffffff-ffff-7fff-8000-000000000002")
    command = ResolveEvent(
        event_id=event.id,
        user_id=event.user_id,
        workspace_id=event.workspace_id,
        goal_ids=(higher, lower, higher),
        resolved_at=RESOLVED_AT,
    )

    await SituationResolver(repository).resolve(command)

    assert repository.received_command is not None
    assert repository.received_command.goal_ids == (lower, higher)
