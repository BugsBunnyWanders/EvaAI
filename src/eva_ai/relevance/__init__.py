from typing import TYPE_CHECKING

from eva_ai.relevance.errors import (
    ClassifierRejectedError,
    ClassifierTransientError,
    EvaluationReviewRequired,
    RelevanceConflictError,
    RelevanceError,
    RelevanceNotFoundError,
    RelevanceScopeError,
)
from eva_ai.relevance.types import (
    AIRelevancePayload,
    ClassifierResult,
    DeterministicRelevancePayload,
    EvaluationAttemptRecord,
    EvaluationAttemptStatus,
    EvaluationContext,
    EvaluationTrigger,
    EventContext,
    FinishEvaluationAttempt,
    GoalContext,
    GoalMatch,
    ReevaluateEvent,
    RelevanceCategory,
    RelevanceDisposition,
    RelevanceMethod,
    RelevanceProvider,
    ScreeningReason,
    SignalDraft,
    SignalKind,
    SignalProducer,
    SignalRecord,
    SituationContext,
    StartEvaluationAttempt,
)

if TYPE_CHECKING:
    from eva_ai.relevance.service import RelevanceEventHandler, RelevanceService

__all__ = [
    "AIRelevancePayload",
    "ClassifierRejectedError",
    "ClassifierResult",
    "ClassifierTransientError",
    "DeterministicRelevancePayload",
    "EvaluationAttemptRecord",
    "EvaluationAttemptStatus",
    "EvaluationContext",
    "EvaluationReviewRequired",
    "EvaluationTrigger",
    "EventContext",
    "FinishEvaluationAttempt",
    "GoalContext",
    "GoalMatch",
    "ReevaluateEvent",
    "RelevanceCategory",
    "RelevanceConflictError",
    "RelevanceDisposition",
    "RelevanceError",
    "RelevanceEventHandler",
    "RelevanceMethod",
    "RelevanceNotFoundError",
    "RelevanceProvider",
    "RelevanceScopeError",
    "RelevanceService",
    "ScreeningReason",
    "SignalDraft",
    "SignalKind",
    "SignalProducer",
    "SignalRecord",
    "SituationContext",
    "StartEvaluationAttempt",
    "backfill_evaluation_key",
    "initial_evaluation_key",
]


def __getattr__(name: str) -> object:
    if name in {
        "RelevanceEventHandler",
        "RelevanceService",
        "backfill_evaluation_key",
        "initial_evaluation_key",
    }:
        from eva_ai.relevance.service import (
            RelevanceEventHandler,
            RelevanceService,
            backfill_evaluation_key,
            initial_evaluation_key,
        )

        return {
            "RelevanceEventHandler": RelevanceEventHandler,
            "RelevanceService": RelevanceService,
            "backfill_evaluation_key": backfill_evaluation_key,
            "initial_evaluation_key": initial_evaluation_key,
        }[name]
    raise AttributeError(name)
