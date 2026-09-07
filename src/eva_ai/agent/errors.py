class AgentError(Exception):
    """Base class for content-free agent failures."""


class AgentScopeError(AgentError):
    pass


class AgentNotFoundError(AgentError):
    pass


class AgentConflictError(AgentError):
    pass


class AgentTransientError(AgentError):
    pass


class AgentPermanentError(AgentError):
    pass


class AgentToolBudgetExceeded(AgentPermanentError):
    pass
