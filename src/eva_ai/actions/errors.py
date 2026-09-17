class ActionError(RuntimeError):
    """Base error whose message is safe to persist and show to operators."""


class ActionValidationError(ActionError):
    """A proposed action failed deterministic validation."""


class ActionScopeError(ActionError):
    """An action resource was unavailable in the authenticated tenant scope."""


class ActionConflictError(ActionError):
    """An action transition lost its claim or conflicted with durable state."""


class ActionProviderError(ActionError):
    """A provider operation failed with provider-controlled details removed."""
