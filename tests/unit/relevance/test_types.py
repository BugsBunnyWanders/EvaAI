from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from eva_ai.relevance.types import (
    AIRelevancePayload,
    ClassifierResult,
    DeterministicRelevancePayload,
    EvaluationAttemptStatus,
    EvaluationTrigger,
    FinishEvaluationAttempt,
    GoalMatch,
    ReevaluateEvent,
    RelevanceCategory,
    RelevanceDisposition,
    ScreeningReason,
    SignalDraft,
    SignalProducer,
)
from eva_ai.situations import GoalContribution

USER_ID = UUID("00000000-0000-7000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000002")
EVENT_ID = UUID("00000000-0000-7000-8000-000000000003")
GOAL_ID = UUID("00000000-0000-7000-8000-000000000004")
ATTEMPT_ID = UUID("00000000-0000-7000-8000-000000000005")
EVALUATION_KEY = UUID("00000000-0000-7000-8000-000000000006")
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def result(*, goal_matches: tuple[GoalMatch, ...] = ()) -> ClassifierResult:
    return ClassifierResult(
        relevance=0.8,
        importance=0.7,
        urgency=0.2,
        confidence=0.9,
        category=RelevanceCategory.GOAL_PROGRESS,
        recommended_action=RelevanceDisposition.NOTIFY,
        reason="The message reports progress on an active goal.",
        goal_matches=goal_matches,
    )


def test_classifier_result_is_strict_bounded_frozen_and_sorts_matches() -> None:
    other_goal = UUID("00000000-0000-7000-8000-000000000007")
    matches = (
        GoalMatch(
            goal_id=other_goal,
            relevance=0.7,
            contribution=GoalContribution.CONTEXT,
            reason=" Context ",
        ),
        GoalMatch(
            goal_id=GOAL_ID,
            relevance=0.9,
            contribution=GoalContribution.SUPPORTS,
            reason=" Direct progress update ",
        ),
    )
    classified = result(goal_matches=matches)

    assert classified.goal_matches[0].goal_id == GOAL_ID
    assert classified.goal_matches[0].reason == "Direct progress update"
    with pytest.raises(ValidationError):
        ClassifierResult.model_validate({**classified.model_dump(), "confidence": 1.01})
    with pytest.raises(ValidationError):
        ClassifierResult.model_validate({**classified.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        classified.reason = "changed"  # type: ignore[misc]


def test_classifier_result_rejects_duplicate_goal_matches() -> None:
    match = GoalMatch(
        goal_id=GOAL_ID,
        relevance=0.9,
        contribution=GoalContribution.SUPPORTS,
        reason="Progress",
    )
    with pytest.raises(ValidationError, match="duplicate Goal matches"):
        result(goal_matches=(match, match))


def test_reevaluation_requires_reason_and_aware_time() -> None:
    with pytest.raises(ValidationError):
        ReevaluateEvent(
            event_id=EVENT_ID,
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            evaluation_key=EVALUATION_KEY,
            reason="   ",
            requested_at=datetime(2026, 9, 1),
        )


def test_signal_draft_requires_consistent_payload_producer_and_reason() -> None:
    match = GoalMatch(
        goal_id=GOAL_ID,
        relevance=0.9,
        contribution=GoalContribution.SUPPORTS,
        reason="Progress",
    )
    payload = AIRelevancePayload(result=result(goal_matches=(match,)), disposition="NOTIFY")
    draft = SignalDraft(
        event_id=EVENT_ID,
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        payload=payload,
        confidence=0.9,
        disposition=RelevanceDisposition.NOTIFY,
        producer=SignalProducer.AI,
        provider="openai",
        model="gpt-test",
        classifier_version="v1",
        policy_version="p1",
        input_digest="a" * 64,
        trigger=EvaluationTrigger.EXPLICIT_REEVALUATION,
        operator_reason=" Try again ",
        evaluation_key=EVALUATION_KEY,
        successful_attempt_id=ATTEMPT_ID,
        goal_matches=(match,),
        created_at=NOW,
    )
    assert draft.operator_reason == "Try again"

    with pytest.raises(ValidationError, match="payload must match producer"):
        draft.model_copy(update={"producer": SignalProducer.DETERMINISTIC}).model_validate(
            {**draft.model_dump(), "producer": SignalProducer.DETERMINISTIC}
        )

    deterministic = DeterministicRelevancePayload(reason=ScreeningReason.IGNORED_LABEL)
    with pytest.raises(ValidationError, match="operator reason"):
        SignalDraft.model_validate(
            {
                **draft.model_dump(),
                "payload": deterministic,
                "producer": SignalProducer.DETERMINISTIC,
                "disposition": RelevanceDisposition.IGNORE,
                "confidence": 1.0,
                "model": None,
                "successful_attempt_id": None,
                "goal_matches": (),
                "trigger": EvaluationTrigger.INITIAL,
            }
        )


@pytest.mark.parametrize(
    ("status", "failure_code", "valid"),
    [
        (EvaluationAttemptStatus.SUCCEEDED, None, True),
        (EvaluationAttemptStatus.SUCCEEDED, "BAD", False),
        (EvaluationAttemptStatus.RETRYABLE_FAILURE, "RATE_LIMIT", True),
        (EvaluationAttemptStatus.PERMANENT_FAILURE, None, False),
    ],
)
def test_finish_attempt_validates_failure_code(
    status: EvaluationAttemptStatus, failure_code: str | None, valid: bool
) -> None:
    values = {
        "attempt_id": ATTEMPT_ID,
        "event_id": EVENT_ID,
        "user_id": USER_ID,
        "workspace_id": WORKSPACE_ID,
        "evaluation_key": EVALUATION_KEY,
        "status": status,
        "failure_code": failure_code,
        "completed_at": NOW,
    }
    if valid:
        assert FinishEvaluationAttempt.model_validate(values).status is status
    else:
        with pytest.raises(ValidationError):
            FinishEvaluationAttempt.model_validate(values)
