class RelevanceError(RuntimeError):
    """Base relevance failure whose message is safe for operator surfaces."""


class RelevanceNotFoundError(RelevanceError):
    pass


class RelevanceScopeError(RelevanceError):
    pass


class RelevanceConflictError(RelevanceError):
    pass


class ClassifierTransientError(RelevanceError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("classifier temporarily unavailable")


class ClassifierRejectedError(RelevanceError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("classifier result unavailable")


class EvaluationReviewRequired(RelevanceError):
    pass
