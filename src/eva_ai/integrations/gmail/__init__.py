"""Google Gmail integration adapters."""

from eva_ai.integrations.gmail.api import (
    GmailActionReauthorizationRequired,
    GmailProviderError,
    GoogleGmailClient,
    GoogleGmailClientFactory,
    InvalidAuthorizedUserCredentials,
)
from eva_ai.integrations.gmail.oauth import (
    GMAIL_COMPOSE_SCOPE,
    GMAIL_CONNECTOR_SCOPES,
    GMAIL_READONLY_SCOPE,
    GoogleDesktopOAuthAuthorizer,
    OAuthAuthorizationError,
)

__all__ = [
    "GMAIL_COMPOSE_SCOPE",
    "GMAIL_CONNECTOR_SCOPES",
    "GMAIL_READONLY_SCOPE",
    "GmailActionReauthorizationRequired",
    "GmailProviderError",
    "GoogleDesktopOAuthAuthorizer",
    "GoogleGmailClient",
    "GoogleGmailClientFactory",
    "InvalidAuthorizedUserCredentials",
    "OAuthAuthorizationError",
]
