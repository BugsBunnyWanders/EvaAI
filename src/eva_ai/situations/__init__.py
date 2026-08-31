from typing import TYPE_CHECKING

from eva_ai.situations.errors import (
    InvalidSituationTransitionError,
    SituationError,
    SituationNotFoundError,
    SituationResolutionError,
    SituationScopeError,
    SituationVersionConflictError,
)
from eva_ai.situations.transitions import validate_situation_transition
from eva_ai.situations.types import (
    AttentionLevel,
    CorrelationKeyKind,
    CorrelationMethod,
    GoalContribution,
    LinkSituationGoal,
    ResolveEvent,
    SituationEventRecord,
    SituationGoalRecord,
    SituationLifecycle,
    SituationRecord,
    SituationResolution,
    SituationSnapshotUpdate,
    SituationType,
)

if TYPE_CHECKING:
    from eva_ai.situations.repository import SituationRepository
    from eva_ai.situations.resolver import SituationResolver
    from eva_ai.situations.service import SituationService

__all__ = [
    "AttentionLevel",
    "CorrelationKeyKind",
    "CorrelationMethod",
    "GoalContribution",
    "InvalidSituationTransitionError",
    "LinkSituationGoal",
    "ResolveEvent",
    "SituationError",
    "SituationEventRecord",
    "SituationGoalRecord",
    "SituationLifecycle",
    "SituationNotFoundError",
    "SituationRecord",
    "SituationRepository",
    "SituationResolver",
    "SituationResolution",
    "SituationResolutionError",
    "SituationScopeError",
    "SituationService",
    "SituationSnapshotUpdate",
    "SituationType",
    "SituationVersionConflictError",
    "validate_situation_transition",
]


def __getattr__(name: str) -> object:
    if name == "SituationRepository":
        from eva_ai.situations.repository import SituationRepository

        return SituationRepository
    if name == "SituationResolver":
        from eva_ai.situations.resolver import SituationResolver

        return SituationResolver
    if name == "SituationService":
        from eva_ai.situations.service import SituationService

        return SituationService
    raise AttributeError(name)
