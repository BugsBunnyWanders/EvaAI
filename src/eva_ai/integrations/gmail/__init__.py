"""Google Gmail integration adapters."""

from eva_ai.integrations.gmail.api import (
    GmailProviderError,
    GoogleGmailClient,
    GoogleGmailClientFactory,
    InvalidAuthorizedUserCredentials,
)
from eva_ai.integrations.gmail.oauth import (
    GMAIL_READONLY_SCOPE,
    GoogleDesktopOAuthAuthorizer,
    OAuthAuthorizationError,
)

__all__ = [
    "GMAIL_READONLY_SCOPE",
    "GmailProviderError",
    "GoogleDesktopOAuthAuthorizer",
    "GoogleGmailClient",
    "GoogleGmailClientFactory",
    "InvalidAuthorizedUserCredentials",
    "OAuthAuthorizationError",
]
