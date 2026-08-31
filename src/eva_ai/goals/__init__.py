from typing import TYPE_CHECKING

from eva_ai.goals.errors import (
    GoalError,
    GoalNotFoundError,
    GoalParentError,
    GoalScopeError,
    InvalidGoalTransitionError,
)
from eva_ai.goals.transitions import validate_goal_transition
from eva_ai.goals.types import (
    SAFE_AUTONOMY_POLICY,
    GoalDraft,
    GoalMode,
    GoalRecord,
    GoalSource,
    GoalStatus,
    GoalUpdate,
    InferredGoalDraft,
    JsonObject,
)

if TYPE_CHECKING:
    from eva_ai.goals.repository import GoalRepository
    from eva_ai.goals.service import GoalService

__all__ = [
    "SAFE_AUTONOMY_POLICY",
    "GoalDraft",
    "GoalError",
    "GoalMode",
    "GoalNotFoundError",
    "GoalParentError",
    "GoalRecord",
    "GoalRepository",
    "GoalService",
    "GoalScopeError",
    "GoalSource",
    "GoalStatus",
    "GoalUpdate",
    "InferredGoalDraft",
    "InvalidGoalTransitionError",
    "JsonObject",
    "validate_goal_transition",
]


def __getattr__(name: str) -> object:
    if name == "GoalRepository":
        from eva_ai.goals.repository import GoalRepository

        return GoalRepository
    if name == "GoalService":
        from eva_ai.goals.service import GoalService

        return GoalService
    raise AttributeError(name)
