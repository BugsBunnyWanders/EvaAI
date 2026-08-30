import asyncio
from pathlib import Path
from typing import Protocol, cast

from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

from eva_ai.connectors.gmail.contracts import AuthorizedUserGrant

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_REQUIRED_SCOPES = (GMAIL_READONLY_SCOPE,)


class OAuthAuthorizationError(RuntimeError):
    """OAuth bootstrap failure with secret-bearing details removed."""


class OAuthCredentials(Protocol):
    def to_json(self) -> str: ...


class OAuthFlow(Protocol):
    def run_local_server(self, **kwargs: str) -> OAuthCredentials: ...


class FlowFactory(Protocol):
    def __call__(self, client_file: str, scopes: tuple[str, ...]) -> OAuthFlow: ...


def _default_flow_factory(client_file: str, scopes: tuple[str, ...]) -> OAuthFlow:
    return cast(OAuthFlow, InstalledAppFlow.from_client_secrets_file(client_file, scopes))


class GoogleDesktopOAuthAuthorizer:
    def __init__(self, flow_factory: FlowFactory = _default_flow_factory) -> None:
        self._flow_factory = flow_factory

    async def authorize(self, client_file: Path, scopes: tuple[str, ...]) -> AuthorizedUserGrant:
        if scopes != _REQUIRED_SCOPES:
            raise ValueError("Gmail authorization requires gmail.readonly only")

        def authorize_sync() -> AuthorizedUserGrant:
            flow = self._flow_factory(str(client_file), _REQUIRED_SCOPES)
            credentials = flow.run_local_server(access_type="offline", prompt="consent")
            return AuthorizedUserGrant(authorized_user_json=credentials.to_json())

        try:
            return await asyncio.to_thread(authorize_sync)
        except Exception:
            raise OAuthAuthorizationError("Google OAuth authorization failed") from None
