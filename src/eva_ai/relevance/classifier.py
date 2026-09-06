import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from random import random
from typing import Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eva_ai.relevance.errors import (
    ClassifierRejectedError,
    ClassifierTransientError,
    EvaluationReviewRequired,
)
from eva_ai.relevance.types import (
    ClassifierResult,
    EvaluationAttemptRecord,
    EvaluationAttemptStatus,
    EvaluationContext,
    EvaluationTrigger,
    FinishEvaluationAttempt,
    StartEvaluationAttempt,
)


class RelevanceClassifier(Protocol):
    async def classify(self, context: EvaluationContext) -> ClassifierResult: ...


class ScriptedRelevanceClassifier:
    """Deterministic test double that makes retry and orchestration tests readable."""

    def __init__(self, script: tuple[ClassifierResult | BaseException, ...]) -> None:
        self._script = deque(script)
        self.contexts: list[EvaluationContext] = []

    async def classify(self, context: EvaluationContext) -> ClassifierResult:
        self.contexts.append(context)
        if not self._script:
            raise AssertionError("classifier script exhausted")
        outcome = self._script.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ClassifierRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    context: EvaluationContext
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_key: UUID
    trigger: EvaluationTrigger
    operator_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def require_explicit_reason(self) -> ClassifierRunRequest:
        explicit = self.trigger is EvaluationTrigger.EXPLICIT_REEVALUATION
        if explicit != (self.operator_reason is not None):
            raise ValueError("operator reason is required exactly for explicit re-evaluation")
        return self


class ClassifierRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    result: ClassifierResult
    successful_attempt_id: UUID


class AttemptRepository(Protocol):
    async def interrupt_started_attempts(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        event_id: UUID,
        evaluation_key: UUID,
        interrupted_at: datetime,
    ) -> int: ...

    async def list_attempts(
        self, *, event_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> tuple[EvaluationAttemptRecord, ...]: ...

    async def start_attempt(self, command: StartEvaluationAttempt) -> EvaluationAttemptRecord: ...

    async def finish_attempt(self, command: FinishEvaluationAttempt) -> EvaluationAttemptRecord: ...


class RelevanceClassifierRunner:
    def __init__(
        self,
        *,
        classifier: RelevanceClassifier,
        attempts: AttemptRepository,
        provider: str,
        model: str,
        classifier_version: str,
        max_attempts: int,
        initial_backoff_seconds: float,
        max_backoff_seconds: float,
        jitter_ratio: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_value: Callable[[], float] = random,
    ) -> None:
        self._classifier = classifier
        self._attempts = attempts
        self._provider = provider
        self._model = model
        self._classifier_version = classifier_version
        self._max_attempts = max_attempts
        self._initial_backoff = initial_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._jitter_ratio = jitter_ratio
        self._sleep = sleep
        self._clock = clock
        self._random = random_value

    async def run(self, request: ClassifierRunRequest) -> ClassifierRunResult:
        context = request.context
        await self._attempts.interrupt_started_attempts(
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            event_id=context.event_id,
            evaluation_key=request.evaluation_key,
            interrupted_at=self._clock(),
        )
        history = await self._attempts.list_attempts(
            event_id=context.event_id,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
        )
        keyed = [item for item in history if item.evaluation_key == request.evaluation_key]
        if keyed and keyed[-1].status is EvaluationAttemptStatus.PERMANENT_FAILURE:
            raise EvaluationReviewRequired("evaluation requires an explicit new key")
        next_number = max((item.attempt_number for item in keyed), default=0) + 1

        while next_number <= self._max_attempts:
            started = await self._attempts.start_attempt(
                StartEvaluationAttempt(
                    event_id=context.event_id,
                    user_id=context.user_id,
                    workspace_id=context.workspace_id,
                    evaluation_key=request.evaluation_key,
                    attempt_number=next_number,
                    trigger=request.trigger,
                    operator_reason=request.operator_reason,
                    provider=self._provider,
                    model=self._model,
                    classifier_version=self._classifier_version,
                    input_digest=request.input_digest,
                    started_at=self._clock(),
                )
            )
            try:
                result = await self._classifier.classify(context)
            except (ClassifierTransientError, ClassifierRejectedError) as error:
                final = next_number >= self._max_attempts
                await self._attempts.finish_attempt(
                    FinishEvaluationAttempt(
                        attempt_id=started.id,
                        event_id=context.event_id,
                        user_id=context.user_id,
                        workspace_id=context.workspace_id,
                        evaluation_key=request.evaluation_key,
                        status=(
                            EvaluationAttemptStatus.PERMANENT_FAILURE
                            if final
                            else EvaluationAttemptStatus.RETRYABLE_FAILURE
                        ),
                        failure_code=error.code,
                        completed_at=self._clock(),
                    )
                )
                if final:
                    raise EvaluationReviewRequired("classifier attempts exhausted") from None
                await self._sleep(self._delay(next_number))
                next_number += 1
                continue

            await self._attempts.finish_attempt(
                FinishEvaluationAttempt(
                    attempt_id=started.id,
                    event_id=context.event_id,
                    user_id=context.user_id,
                    workspace_id=context.workspace_id,
                    evaluation_key=request.evaluation_key,
                    status=EvaluationAttemptStatus.SUCCEEDED,
                    completed_at=self._clock(),
                )
            )
            return ClassifierRunResult(result=result, successful_attempt_id=started.id)
        raise EvaluationReviewRequired("classifier attempts exhausted")

    def _delay(self, attempt_number: int) -> float:
        base = min(self._max_backoff, self._initial_backoff * (2 ** (attempt_number - 1)))
        factor = 1.0 + self._jitter_ratio * ((2.0 * self._random()) - 1.0)
        return cast(float, max(0.0, min(self._max_backoff, base * factor)))
