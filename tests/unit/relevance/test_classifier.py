from datetime import UTC, datetime
from uuid import uuid7

import pytest

from eva_ai.relevance.classifier import ScriptedRelevanceClassifier
from eva_ai.relevance.errors import ClassifierTransientError
from eva_ai.relevance.types import (
    ClassifierResult,
    EvaluationContext,
    EventContext,
    RelevanceCategory,
    RelevanceDisposition,
)


def context() -> EvaluationContext:
    return EvaluationContext(
        event_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        event=EventContext(
            source="gmail",
            event_type="email.received",
            occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
            sender_name="Eva",
            sender_address="eva@example.com",
            subject="Update",
            snippet="Preview",
            label_ids=("INBOX",),
            plain_text="Body",
        ),
        goals=(),
        situations=(),
    )


def result(action: RelevanceDisposition) -> ClassifierResult:
    return ClassifierResult(
        relevance=0.5,
        importance=0.5,
        urgency=0.5,
        confidence=0.9,
        category=RelevanceCategory.INFORMATIONAL,
        recommended_action=action,
        reason="Relevant context",
    )


async def test_scripted_classifier_returns_results_and_errors_in_order() -> None:
    first = result(RelevanceDisposition.RECORD)
    failure = ClassifierTransientError("TIMEOUT")
    classifier = ScriptedRelevanceClassifier((first, failure))
    value = context()

    assert await classifier.classify(value) == first
    with pytest.raises(ClassifierTransientError) as raised:
        await classifier.classify(value)
    assert raised.value.code == "TIMEOUT"
    assert classifier.contexts == [value, value]
    with pytest.raises(AssertionError, match="script exhausted"):
        await classifier.classify(value)
