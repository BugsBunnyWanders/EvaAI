from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid7

import pytest
from pydantic import ValidationError

from eva_ai.situations import (
    AttentionLevel,
    CorrelationMethod,
    GoalContribution,
    LinkSituationGoal,
    ResolveEvent,
    SituationEventRecord,
    SituationGoalRecord,
    SituationLifecycle,
    SituationRecord,
    SituationSnapshotUpdate,
    SituationType,
)

NOW = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)


def situation_values() -> dict[str, object]:
    return {
        "id": uuid7(),
        "user_id": uuid7(),
        "workspace_id": uuid7(),
        "type": SituationType.EMAIL_THREAD,
        "title": "  Visa application  ",
        "lifecycle": SituationLifecycle.OPEN,
        "attention": AttentionLevel.NORMAL,
        "summary": " Waiting for an appointment. ",
        "current_state": "  NEW  ",
        "next_action": None,
        "next_expected": None,
        "version": 1,
        "last_activity_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }


def test_situation_record_normalizes_display_text_and_is_frozen() -> None:
    record = SituationRecord(**situation_values())

    assert record.title == "Visa application"
    assert record.summary == "Waiting for an appointment."
    assert record.current_state == "NEW"
    with pytest.raises(ValidationError):
        record.title = "Changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", " "),
        ("title", "x" * 301),
        ("summary", "x" * 2001),
        ("current_state", " "),
        ("current_state", "x" * 101),
        ("next_action", "x" * 1001),
        ("next_expected", "x" * 1001),
        ("version", 0),
    ],
)
def test_situation_record_rejects_invalid_snapshot_boundaries(field: str, value: object) -> None:
    values = situation_values()
    values[field] = value

    with pytest.raises(ValidationError):
        SituationRecord(**values)


def test_situation_record_allows_an_empty_summary() -> None:
    assert SituationRecord(**{**situation_values(), "summary": ""}).summary == ""


@pytest.mark.parametrize(
    "field",
    ["last_activity_at", "created_at", "updated_at"],
)
def test_situation_record_rejects_naive_timestamps(field: str) -> None:
    values = situation_values()
    values[field] = datetime(2026, 8, 31)

    with pytest.raises(ValidationError):
        SituationRecord(**values)


def test_snapshot_update_requires_positive_version_and_aware_timestamp() -> None:
    values = {
        "user_id": uuid7(),
        "workspace_id": uuid7(),
        "situation_id": uuid7(),
        "expected_version": 1,
        "title": "Thread",
        "summary": "Summary",
        "current_state": "ACTIVE",
        "updated_at": NOW,
    }
    assert SituationSnapshotUpdate(**values).expected_version == 1

    with pytest.raises(ValidationError):
        SituationSnapshotUpdate(**{**values, "expected_version": 0})
    with pytest.raises(ValidationError):
        SituationSnapshotUpdate(**{**values, "updated_at": datetime(2026, 8, 31)})


def test_resolve_event_sorts_and_deduplicates_goal_ids() -> None:
    higher = UUID("ffffffff-ffff-7fff-8000-000000000002")
    lower = UUID("00000000-0000-7000-8000-000000000001")

    command = ResolveEvent(
        event_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        goal_ids=(higher, lower, higher),
        resolved_at=NOW,
    )

    assert command.goal_ids == (lower, higher)


def test_resolve_event_rejects_naive_resolution_time() -> None:
    with pytest.raises(ValidationError):
        ResolveEvent(
            event_id=uuid7(),
            user_id=uuid7(),
            workspace_id=uuid7(),
            resolved_at=datetime(2026, 8, 31),
        )


@pytest.mark.parametrize("relevance", [Decimal("-0.001"), Decimal("1.001")])
def test_goal_link_rejects_out_of_range_relevance(relevance: Decimal) -> None:
    with pytest.raises(ValidationError):
        LinkSituationGoal(
            user_id=uuid7(),
            workspace_id=uuid7(),
            situation_id=uuid7(),
            goal_id=uuid7(),
            relevance=relevance,
            contribution=GoalContribution.CONTEXT,
            linked_at=NOW,
        )


def test_goal_link_normalizes_optional_reasoning() -> None:
    values = {
        "user_id": uuid7(),
        "workspace_id": uuid7(),
        "situation_id": uuid7(),
        "goal_id": uuid7(),
        "relevance": Decimal("0.75"),
        "contribution": GoalContribution.SUPPORTS,
        "linked_at": NOW,
    }
    assert LinkSituationGoal(**values, reasoning=" useful ").reasoning == "useful"
    assert LinkSituationGoal(**values).reasoning is None

    with pytest.raises(ValidationError):
        LinkSituationGoal(**values, reasoning=" ")
    with pytest.raises(ValidationError):
        LinkSituationGoal(**values, reasoning="x" * 1001)


def test_relationship_records_expose_metadata_without_event_payload() -> None:
    event_record = SituationEventRecord(
        situation_id=uuid7(),
        event_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        correlation_method=CorrelationMethod.DETERMINISTIC_KEY,
        correlation_key="gmail-thread:thread-1",
        event_occurred_at=NOW,
        linked_at=NOW,
    )
    goal_record = SituationGoalRecord(
        situation_id=uuid7(),
        goal_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        relevance=Decimal("1"),
        contribution=GoalContribution.CONTEXT,
        reasoning=None,
        linked_at=NOW,
    )

    assert "payload" not in type(event_record).model_fields
    assert goal_record.reasoning is None
