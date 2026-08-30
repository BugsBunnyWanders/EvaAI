import logging
import threading
from pathlib import Path

import pytest

from eva_ai.connectors.gmail.contracts import AuthorizedUserGrant
from eva_ai.integrations.gmail.oauth import (
    GMAIL_READONLY_SCOPE,
    GoogleDesktopOAuthAuthorizer,
    OAuthAuthorizationError,
)


class FakeCredentials:
    def __init__(self, authorized_user_json: str, main_thread_id: int) -> None:
        self._authorized_user_json = authorized_user_json
        self._main_thread_id = main_thread_id

    def to_json(self) -> str:
        assert threading.get_ident() != self._main_thread_id
        return self._authorized_user_json


class FakeFlow:
    def __init__(self, credentials: FakeCredentials, main_thread_id: int) -> None:
        self._credentials = credentials
        self._main_thread_id = main_thread_id
        self.local_server_calls: list[dict[str, str]] = []

    def run_local_server(self, **kwargs: str) -> FakeCredentials:
        assert threading.get_ident() != self._main_thread_id
        self.local_server_calls.append(kwargs)
        return self._credentials


@pytest.mark.asyncio
async def test_authorize_requests_only_offline_readonly_access_without_logging_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fails if OAuth broadens scope/options or exposes the resulting credential JSON."""
    main_thread_id = threading.get_ident()
    secret_json = '{"refresh_token":"never-log-this"}'
    flow = FakeFlow(FakeCredentials(secret_json, main_thread_id), main_thread_id)
    factory_calls: list[tuple[str, tuple[str, ...]]] = []

    def flow_factory(client_file: str, scopes: tuple[str, ...]) -> FakeFlow:
        assert threading.get_ident() != main_thread_id
        factory_calls.append((client_file, scopes))
        return flow

    caplog.set_level(logging.DEBUG)
    grant = await GoogleDesktopOAuthAuthorizer(flow_factory=flow_factory).authorize(
        Path("/private/oauth-client.json"),
        (GMAIL_READONLY_SCOPE,),
    )

    assert grant == AuthorizedUserGrant(authorized_user_json=secret_json)
    assert factory_calls == [
        ("/private/oauth-client.json", ("https://www.googleapis.com/auth/gmail.readonly",))
    ]
    assert flow.local_server_calls == [{"access_type": "offline", "prompt": "consent"}]
    assert secret_json not in caplog.text
    assert "never-log-this" not in repr(grant)


@pytest.mark.asyncio
async def test_authorize_rejects_any_scope_other_than_gmail_readonly() -> None:
    """Fails if a caller can expand the ingestion connector's OAuth authority."""
    called = False

    def flow_factory(client_file: str, scopes: tuple[str, ...]) -> FakeFlow:
        del client_file, scopes
        nonlocal called
        called = True
        raise AssertionError("flow construction must not run for a forbidden scope")

    with pytest.raises(ValueError, match="^Gmail authorization requires gmail.readonly only$"):
        await GoogleDesktopOAuthAuthorizer(flow_factory=flow_factory).authorize(
            Path("oauth-client.json"),
            ("https://mail.google.com/",),
        )

    assert called is False


@pytest.mark.asyncio
async def test_authorize_maps_provider_failure_without_exposing_secret_text() -> None:
    """Fails if OAuth client secrets or provider response details escape in exceptions."""

    def failing_flow_factory(client_file: str, scopes: tuple[str, ...]) -> FakeFlow:
        del client_file, scopes
        raise RuntimeError("client_secret=private-provider-body")

    with pytest.raises(OAuthAuthorizationError) as raised:
        await GoogleDesktopOAuthAuthorizer(flow_factory=failing_flow_factory).authorize(
            Path("oauth-client.json"), (GMAIL_READONLY_SCOPE,)
        )

    assert str(raised.value) == "Google OAuth authorization failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
