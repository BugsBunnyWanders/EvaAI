import pytest

from eva_ai.goals import GoalStatus, InvalidGoalTransitionError, validate_goal_transition

ALLOWED_TRANSITIONS = {
    GoalStatus.CANDIDATE: {GoalStatus.ACTIVE, GoalStatus.ABANDONED},
    GoalStatus.ACTIVE: {GoalStatus.PAUSED, GoalStatus.COMPLETED, GoalStatus.ABANDONED},
    GoalStatus.PAUSED: {GoalStatus.ACTIVE, GoalStatus.COMPLETED, GoalStatus.ABANDONED},
    GoalStatus.COMPLETED: set(),
    GoalStatus.ABANDONED: set(),
}


@pytest.mark.parametrize("current", list(GoalStatus))
@pytest.mark.parametrize("requested", list(GoalStatus))
def test_goal_transition_matrix(current: GoalStatus, requested: GoalStatus) -> None:
    if requested == current or requested in ALLOWED_TRANSITIONS[current]:
        validate_goal_transition(current, requested)
    else:
        with pytest.raises(InvalidGoalTransitionError, match="Goal status transition is invalid"):
            validate_goal_transition(current, requested)
