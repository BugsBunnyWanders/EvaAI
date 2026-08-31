class SituationError(Exception):
    """Base class for safe Situation-domain failures."""


class SituationNotFoundError(SituationError):
    def __init__(self) -> None:
        super().__init__("Situation was not found")


class SituationScopeError(SituationError):
    def __init__(self) -> None:
        super().__init__("Situation scope is invalid")


class InvalidSituationTransitionError(SituationError):
    def __init__(self) -> None:
        super().__init__("Situation lifecycle transition is invalid")


class SituationVersionConflictError(SituationError):
    def __init__(self) -> None:
        super().__init__("Situation snapshot version is stale")


class SituationResolutionError(SituationError):
    def __init__(self) -> None:
        super().__init__("Event cannot be resolved into a Situation")
