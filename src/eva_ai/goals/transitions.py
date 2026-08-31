from eva_ai.goals.errors import InvalidGoalTransitionError
from eva_ai.goals.types import GoalStatus

_ALLOWED_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.CANDIDATE: frozenset({GoalStatus.ACTIVE, GoalStatus.ABANDONED}),
    GoalStatus.ACTIVE: frozenset({GoalStatus.PAUSED, GoalStatus.COMPLETED, GoalStatus.ABANDONED}),
    GoalStatus.PAUSED: frozenset({GoalStatus.ACTIVE, GoalStatus.COMPLETED, GoalStatus.ABANDONED}),
    # Completed and abandoned Goals are terminal so past intent is never silently reopened.
    GoalStatus.COMPLETED: frozenset(),
    GoalStatus.ABANDONED: frozenset(),
}


def validate_goal_transition(current: GoalStatus, requested: GoalStatus) -> None:
    if requested == current:
        return
    if requested not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidGoalTransitionError
