from dataclasses import dataclass

from eva_ai.relevance.types import ClassifierResult, RelevanceDisposition


@dataclass(frozen=True, slots=True)
class RoutingThresholds:
    notify_relevance: float
    notify_confidence: float
    notify_importance_or_urgency: float
    investigate_relevance: float
    investigate_confidence: float
    ignore_relevance: float
    ignore_confidence: float


class RelevanceRoutingPolicy:
    def __init__(self, thresholds: RoutingThresholds, version: str) -> None:
        self.thresholds = thresholds
        self.version = version

    def route(self, result: ClassifierResult) -> RelevanceDisposition:
        if self._should_notify(result):
            return RelevanceDisposition.NOTIFY
        if self._should_investigate(result):
            return RelevanceDisposition.INVESTIGATE
        if self._should_ignore(result):
            return RelevanceDisposition.IGNORE
        return RelevanceDisposition.RECORD

    def _should_notify(self, result: ClassifierResult) -> bool:
        return (
            result.relevance >= self.thresholds.notify_relevance
            and result.confidence >= self.thresholds.notify_confidence
            and max(result.importance, result.urgency)
            >= self.thresholds.notify_importance_or_urgency
        )

    def _should_investigate(self, result: ClassifierResult) -> bool:
        return (
            result.recommended_action is RelevanceDisposition.INVESTIGATE
            and result.relevance >= self.thresholds.investigate_relevance
            and result.confidence >= self.thresholds.investigate_confidence
        )

    def _should_ignore(self, result: ClassifierResult) -> bool:
        return (
            result.recommended_action is RelevanceDisposition.IGNORE
            and result.relevance <= self.thresholds.ignore_relevance
            and result.confidence >= self.thresholds.ignore_confidence
        )
