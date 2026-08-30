import threading
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from google.api_core.exceptions import NotFound

from eva_ai.integrations.gcp.secret_manager import (
    AccessSecretVersionResponse,
    GoogleSecretManagerCredentialStore,
)


class FakeSecretManagerClient:
    def __init__(self, *, secret_exists: bool, stored_value: bytes = b"stored-secret") -> None:
        self.main_thread_id = threading.get_ident()
        self.secret_exists = secret_exists
        self.stored_value = stored_value
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _record(self, operation: str, request: dict[str, object]) -> None:
        assert threading.get_ident() != self.main_thread_id
        self.calls.append((operation, request))

    def get_secret(self, *, request: dict[str, object]) -> object:
        self._record("get_secret", request)
        if not self.secret_exists:
            raise NotFound("secret absent")  # type: ignore[no-untyped-call]
        return SimpleNamespace(name=request["name"])

    def create_secret(self, *, request: dict[str, object]) -> object:
        self._record("create_secret", request)
        return SimpleNamespace(name=f"{request['parent']}/secrets/{request['secret_id']}")

    def add_secret_version(self, *, request: dict[str, object]) -> object:
        self._record("add_secret_version", request)
        return SimpleNamespace(name=f"{request['parent']}/versions/1")

    def access_secret_version(self, *, request: dict[str, object]) -> AccessSecretVersionResponse:
        self._record("access_secret_version", request)
        return cast(
            AccessSecretVersionResponse,
            SimpleNamespace(name=request["name"], payload=SimpleNamespace(data=self.stored_value)),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("secret_exists", [False, True])
async def test_put_creates_only_when_absent_and_always_adds_utf8_version(
    secret_exists: bool,
) -> None:
    """Fails on duplicate creation, missing rotation, wrong name, or non-UTF-8 payload."""
    connector_id = UUID("0191cafe-7b00-7000-8000-000000000001")
    client = FakeSecretManagerClient(secret_exists=secret_exists)
    store = GoogleSecretManagerCredentialStore("evaai-507018", client)

    reference = await store.put(connector_id, '{"refresh_token":"synthetic"}')

    secret_name = (
        "projects/evaai-507018/secrets/eva-gmail-oauth-0191cafe-7b00-7000-8000-000000000001"
    )
    assert reference == secret_name
    assert client.calls[0] == ("get_secret", {"name": secret_name})
    expected_version_call = (
        "add_secret_version",
        {
            "parent": secret_name,
            "payload": {"data": b'{"refresh_token":"synthetic"}'},
        },
    )
    if secret_exists:
        assert client.calls == [client.calls[0], expected_version_call]
    else:
        assert client.calls == [
            client.calls[0],
            (
                "create_secret",
                {
                    "parent": "projects/evaai-507018",
                    "secret_id": "eva-gmail-oauth-0191cafe-7b00-7000-8000-000000000001",
                    "secret": {"replication": {"automatic": {}}},
                },
            ),
            expected_version_call,
        ]


@pytest.mark.asyncio
async def test_get_loads_latest_secret_version_and_decodes_utf8() -> None:
    """Fails if retrieval pins an old credential version or returns raw bytes."""
    client = FakeSecretManagerClient(secret_exists=True, stored_value="credential-✓".encode())
    store = GoogleSecretManagerCredentialStore("evaai-507018", client)
    reference = "projects/evaai-507018/secrets/eva-gmail-oauth-connector"

    value = await store.get(reference)

    assert value == "credential-✓"
    assert client.calls == [
        (
            "access_secret_version",
            {"name": f"{reference}/versions/latest"},
        )
    ]
