class ConversationError(Exception):
    """Base conversation processing error."""


class ConversationScopeError(ConversationError):
    """Raised when the conversation subject is missing or outside the authenticated scope."""


class ConversationConflictError(ConversationError):
    """Raised when a claim or turn transition is stale."""


class ConversationPermanentError(ConversationError):
    """Safe terminal conversation-agent failure."""


class ConversationTransientError(ConversationError):
    """Safe retryable conversation-agent failure."""
