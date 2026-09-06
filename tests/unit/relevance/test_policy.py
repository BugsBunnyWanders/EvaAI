import pytest

from eva_ai.relevance.policy import (
    RelevanceRoutingPolicy,
    RoutingThresholds,
)
from eva_ai.relevance.types import (
    ClassifierResult,
    RelevanceCategory,
    RelevanceDisposition,
)

DEFAULTS = RoutingThresholds(
    notify_relevance=0.75,
    notify_confidence=0.70,
    notify_importance_or_urgency=0.65,
    investigate_relevance=0.60,
    investigate_confidence=0.65,
    ignore_relevance=0.20,
    ignore_confidence=0.80,
)


def result(
    *,
    relevance: float,
    confidence: float,
    importance: float = 0.0,
    urgency: float = 0.0,
    recommended_action: RelevanceDisposition = RelevanceDisposition.RECORD,
) -> ClassifierResult:
    return ClassifierResult(
        relevance=relevance,
        importance=importance,
        urgency=urgency,
        confidence=confidence,
        category=RelevanceCategory.OTHER,
        recommended_action=recommended_action,
        reason="Policy test",
    )


@pytest.mark.parametrize(
    ("classified", "expected"),
    [
        (
            result(relevance=0.75, confidence=0.70, importance=0.65),
            RelevanceDisposition.NOTIFY,
        ),
        (
            result(
                relevance=0.60,
                confidence=0.65,
                recommended_action=RelevanceDisposition.INVESTIGATE,
            ),
            RelevanceDisposition.INVESTIGATE,
        ),
        (
            result(
                relevance=0.20,
                confidence=0.80,
                recommended_action=RelevanceDisposition.IGNORE,
            ),
            RelevanceDisposition.IGNORE,
        ),
        (
            result(
                relevance=0.95,
                confidence=0.40,
                importance=0.95,
                recommended_action=RelevanceDisposition.NOTIFY,
            ),
            RelevanceDisposition.RECORD,
        ),
    ],
)
def test_policy_owns_route_at_exact_boundaries(
    classified: ClassifierResult, expected: RelevanceDisposition
) -> None:
    assert RelevanceRoutingPolicy(DEFAULTS, "p1").route(classified) is expected


def test_notify_precedes_advisory_investigate() -> None:
    classified = result(
        relevance=0.9,
        confidence=0.9,
        urgency=0.9,
        recommended_action=RelevanceDisposition.INVESTIGATE,
    )
    assert RelevanceRoutingPolicy(DEFAULTS, "p1").route(classified) is RelevanceDisposition.NOTIFY


def test_advisory_ignore_cannot_bypass_thresholds() -> None:
    classified = result(
        relevance=0.21,
        confidence=0.99,
        recommended_action=RelevanceDisposition.IGNORE,
    )
    assert RelevanceRoutingPolicy(DEFAULTS, "p1").route(classified) is RelevanceDisposition.RECORD
