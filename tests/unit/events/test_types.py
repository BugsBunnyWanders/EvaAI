from datetime import UTC, datetime
from uuid import UUID, uuid7

import pytest
from pydantic import ValidationError

from eva_ai.events.types import EventAvailableMessage, NewEvent, PrincipalType


def valid_event() -> dict[str, object]:
    return {
        "user_id": uuid7(),
        "workspace_id": uuid7(),
        "source": "gmail",
        "event_type": "email.received",
        "idempotency_key": "gmail:message-123",
        "occurred_at": datetime(2026, 8, 30, 8, 0, tzinfo=UTC),
        "principal_type": PrincipalType.USER,
        "payload": {"message_id": "message-123"},
    }


def test_new_event_is_immutable_and_generates_uuid7() -> None:
    event = NewEvent.model_validate(valid_event())
    assert event.id.version == 7
    with pytest.raises(ValidationError):
        event.source = "calendar"  # type: ignore[misc]


@pytest.mark.parametrize("field", ["source", "event_type", "idempotency_key"])
def test_new_event_rejects_blank_identifiers(field: str) -> None:
    values = valid_event()
    values[field] = "   "
    with pytest.raises(ValidationError):
        NewEvent.model_validate(values)


def test_new_event_rejects_naive_datetimes_and_non_positive_versions() -> None:
    values = valid_event() | {"occurred_at": datetime(2026, 8, 30), "schema_version": 0}
    with pytest.raises(ValidationError):
        NewEvent.model_validate(values)


def test_event_available_message_round_trips_as_json() -> None:
    message = EventAvailableMessage(
        outbox_message_id=uuid7(),
        event_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        event_type="email.received",
        schema_version=1,
    )
    decoded = EventAvailableMessage.model_validate_json(message.model_dump_json())
    assert decoded == message
    assert isinstance(decoded.event_id, UUID)
