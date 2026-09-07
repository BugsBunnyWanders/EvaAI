class NotificationError(Exception):
    """Base notification lifecycle error."""


class NotificationNotFoundError(NotificationError):
    """Raised when a scoped Notification does not exist."""


class NotificationConflictError(NotificationError):
    """Raised when a Notification cannot transition from its current state."""


class NotificationScopeError(NotificationError):
    """Raised when the delivery target is unavailable or outside scope."""
