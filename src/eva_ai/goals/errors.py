class GoalError(Exception):
    """Base class for safe Goal-domain failures."""


class GoalNotFoundError(GoalError):
    def __init__(self) -> None:
        super().__init__("Goal was not found")


class GoalScopeError(GoalError):
    def __init__(self) -> None:
        super().__init__("Goal scope is invalid")


class InvalidGoalTransitionError(GoalError):
    def __init__(self) -> None:
        super().__init__("Goal status transition is invalid")


class GoalParentError(GoalError):
    def __init__(self) -> None:
        super().__init__("Goal parent is invalid")
