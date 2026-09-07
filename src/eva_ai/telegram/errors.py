class TelegramError(Exception):
    """Base exception for Telegram integration failures."""


class PairingCodeInvalidError(TelegramError):
    """Raised when a pairing code is missing, expired, or already consumed."""


class TelegramAccountConflictError(TelegramError):
    """Raised when a provider identity is already paired to another Eva user."""


class TelegramAccountNotFoundError(TelegramError):
    """Raised when a scoped Telegram account does not exist."""


class TelegramAuthenticationError(TelegramError):
    """Raised when a webhook request cannot be authenticated."""


class TelegramProviderError(TelegramError):
    """Sanitized Telegram provider failure."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
