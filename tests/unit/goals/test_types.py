import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid7

import pytest
from pydantic import ValidationError

from eva_ai.goals import (
    GoalDraft,
    GoalMode,
    GoalRecord,
    GoalSource,
    GoalStatus,
    GoalUpdate,
    InferredGoalDraft,
)


def goal_values() -> dict[str, object]:
    return {
        "user_id": uuid7(),
        "workspace_id": uuid7(),
        "title": "  Book a holiday  ",
        "objective": "  Take one restorative week off  ",
        "domain": "  personal  ",
        "mode": GoalMode.ACHIEVE,
    }


def test_goal_draft_normalizes_text_and_criteria() -> None:
    draft = GoalDraft(
        **goal_values(),
        success_criteria=("  Flights booked ", "Hotel confirmed"),
    )

    assert draft.title == "Book a holiday"
    assert draft.objective == "Take one restorative week off"
    assert draft.domain == "personal"
    assert draft.success_criteria == ("Flights booked", "Hotel confirmed")
    assert draft.priority == 50


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", " "),
        ("title", "x" * 201),
        ("objective", "x" * 4001),
        ("domain", "x" * 101),
        ("priority", -1),
        ("priority", 101),
    ],
)
def test_goal_draft_rejects_invalid_field_boundaries(field: str, value: object) -> None:
    values = goal_values()
    values[field] = value

    with pytest.raises(ValidationError):
        GoalDraft(**values)


def test_goal_draft_rejects_too_many_or_invalid_success_criteria() -> None:
    with pytest.raises(ValidationError):
        GoalDraft(**goal_values(), success_criteria=tuple(str(index) for index in range(21)))

    with pytest.raises(ValidationError):
        GoalDraft(**goal_values(), success_criteria=(" ",))

    with pytest.raises(ValidationError):
        GoalDraft(**goal_values(), success_criteria=("x" * 501,))


def test_goal_draft_enforces_compact_utf8_constraint_limit() -> None:
    within_limit = {"note": "x" * 8181}
    assert len(json.dumps(within_limit, separators=(",", ":")).encode()) == 8192
    assert GoalDraft(**goal_values(), constraints=within_limit).constraints == within_limit

    with pytest.raises(ValidationError):
        GoalDraft(**goal_values(), constraints={"note": "x" * 8182})


@pytest.mark.parametrize("confidence", [Decimal("-0.001"), Decimal("1.001")])
def test_inferred_goal_rejects_out_of_range_confidence(confidence: Decimal) -> None:
    with pytest.raises(ValidationError):
        InferredGoalDraft(**goal_values(), confidence=confidence)


def test_goal_update_requires_a_real_change_and_explicit_parent_clear() -> None:
    scope = {"user_id": uuid7(), "workspace_id": uuid7(), "goal_id": uuid7()}

    with pytest.raises(ValidationError):
        GoalUpdate(**scope)
    with pytest.raises(ValidationError):
        GoalUpdate(**scope, clear_parent=False)
    with pytest.raises(ValidationError):
        GoalUpdate(**scope, title=None)
    with pytest.raises(ValidationError):
        GoalUpdate(**scope, parent_goal_id=uuid7(), clear_parent=True)

    assert GoalUpdate(**scope, success_criteria=()).success_criteria == ()
    assert GoalUpdate(**scope, clear_parent=True).clear_parent is True


def test_goal_record_is_frozen_and_owns_its_safe_autonomy_mapping() -> None:
    now = datetime(2026, 8, 31, tzinfo=UTC)
    common = {
        **goal_values(),
        "id": uuid7(),
        "priority": 50,
        "status": GoalStatus.ACTIVE,
        "success_criteria": (),
        "constraints": {},
        "source": GoalSource.USER_EXPLICIT,
        "confidence": Decimal("1"),
        "parent_goal_id": None,
        "created_at": now,
        "updated_at": now,
    }
    first = GoalRecord(**common)
    second = GoalRecord(**common)

    assert first.autonomy_policy == {"mode": "REQUIRE_APPROVAL"}
    assert first.autonomy_policy is not second.autonomy_policy
    with pytest.raises(ValidationError):
        first.title = "Changed"  # type: ignore[misc]


def test_goal_record_rejects_naive_timestamps() -> None:
    now = datetime(2026, 8, 31, tzinfo=UTC)
    values = {
        **goal_values(),
        "id": uuid7(),
        "priority": 50,
        "status": GoalStatus.ACTIVE,
        "success_criteria": (),
        "constraints": {},
        "source": GoalSource.USER_EXPLICIT,
        "confidence": Decimal("1"),
        "parent_goal_id": None,
        "created_at": datetime(2026, 8, 31),
        "updated_at": now,
    }

    with pytest.raises(ValidationError):
        GoalRecord(**values)


def test_goal_record_rejects_any_autonomy_mode_other_than_approval() -> None:
    now = datetime(2026, 8, 31, tzinfo=UTC)

    with pytest.raises(ValidationError):
        GoalRecord(
            **goal_values(),
            id=uuid7(),
            priority=50,
            status=GoalStatus.ACTIVE,
            success_criteria=(),
            constraints={},
            autonomy_policy={"mode": "AUTONOMOUS"},
            source=GoalSource.USER_EXPLICIT,
            confidence=Decimal("1"),
            parent_goal_id=None,
            created_at=now,
            updated_at=now,
        )
