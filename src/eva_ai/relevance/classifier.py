from collections import deque
from typing import Protocol

from eva_ai.relevance.types import ClassifierResult, EvaluationContext


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
