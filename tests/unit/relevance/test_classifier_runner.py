from datetime import UTC, datetime
from uuid import UUID

import pytest

from eva_ai.relevance.classifier import (
    ClassifierRunRequest,
    RelevanceClassifierRunner,
    ScriptedRelevanceClassifier,
)
from eva_ai.relevance.errors import ClassifierTransientError, EvaluationReviewRequired
from eva_ai.relevance.types import (
    EvaluationAttemptRecord,
    EvaluationAttemptStatus,
    EvaluationTrigger,
    FinishEvaluationAttempt,
    RelevanceDisposition,
    StartEvaluationAttempt,
)
from tests.unit.relevance.test_classifier import context, result

NOW = datetime(2026, 9, 1, tzinfo=UTC)


class RecordingAttempts:
    def __init__(self) -> None:
        self.records: list[EvaluationAttemptRecord] = []

    async def interrupt_started_attempts(self, **kwargs: object) -> int:
        return 0

    async def list_attempts(
        self, *, event_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> tuple[EvaluationAttemptRecord, ...]:
        return tuple(self.records)

    async def start_attempt(self, command: StartEvaluationAttempt) -> EvaluationAttemptRecord:
        record = EvaluationAttemptRecord(
            **command.model_dump(),
            status=EvaluationAttemptStatus.STARTED,
            failure_code=None,
            completed_at=None,
        )
        self.records.append(record)
        return record

    async def finish_attempt(self, command: FinishEvaluationAttempt) -> EvaluationAttemptRecord:
        current = next(item for item in self.records if item.id == command.attempt_id)
        finished = current.model_copy(update=command.model_dump(exclude={"attempt_id"}))
        self.records[self.records.index(current)] = finished
        return finished


async def test_runner_persists_attempts_and_retries_with_backoff() -> None:
    expected = result(RelevanceDisposition.NOTIFY)
    classifier = ScriptedRelevanceClassifier((ClassifierTransientError("RATE_LIMIT"), expected))
    attempts = RecordingAttempts()
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    value = context()
    runner = RelevanceClassifierRunner(
        classifier=classifier,
        attempts=attempts,
        provider="openai",
        model="gpt-5.6-luna",
        classifier_version="v1",
        max_attempts=3,
        initial_backoff_seconds=2,
        max_backoff_seconds=30,
        jitter_ratio=0,
        sleep=record_sleep,
        clock=lambda: NOW,
    )
    completed = await runner.run(
        ClassifierRunRequest(
            context=value,
            input_digest="a" * 64,
            evaluation_key=UUID(int=1),
            trigger=EvaluationTrigger.INITIAL,
        )
    )

    assert completed.result == expected
    assert completed.successful_attempt_id == attempts.records[1].id
    assert [item.status for item in attempts.records] == [
        EvaluationAttemptStatus.RETRYABLE_FAILURE,
        EvaluationAttemptStatus.SUCCEEDED,
    ]
    assert [item.attempt_number for item in attempts.records] == [1, 2]
    assert sleeps == [2.0]


async def test_runner_exhaustion_is_review_required_and_content_free() -> None:
    failure = ClassifierTransientError("secret provider content")
    attempts = RecordingAttempts()
    runner = RelevanceClassifierRunner(
        classifier=ScriptedRelevanceClassifier((failure, failure, failure)),
        attempts=attempts,
        provider="openai",
        model="model",
        classifier_version="v1",
        max_attempts=3,
        initial_backoff_seconds=2,
        max_backoff_seconds=30,
        jitter_ratio=0,
        sleep=lambda _: _noop(),
        clock=lambda: NOW,
    )

    with pytest.raises(EvaluationReviewRequired, match="attempts exhausted"):
        await runner.run(
            ClassifierRunRequest(
                context=context(),
                input_digest="b" * 64,
                evaluation_key=UUID(int=2),
                trigger=EvaluationTrigger.BACKFILL,
            )
        )

    assert attempts.records[-1].status is EvaluationAttemptStatus.PERMANENT_FAILURE
    assert [item.failure_code for item in attempts.records] == [
        "secret provider content",
        "secret provider content",
        "secret provider content",
    ]


async def _noop() -> None:
    return None
