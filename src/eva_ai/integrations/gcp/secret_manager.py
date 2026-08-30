import asyncio
from typing import Protocol, cast
from uuid import UUID

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import secretmanager


class SecretManagerProviderError(RuntimeError):
    """Retryable Secret Manager failure with sensitive details removed."""


class SecretPayload(Protocol):
    data: bytes


class AccessSecretVersionResponse(Protocol):
    payload: SecretPayload


class SecretManagerClient(Protocol):
    def get_secret(self, *, request: dict[str, object]) -> object: ...

    def create_secret(self, *, request: dict[str, object]) -> object: ...

    def add_secret_version(self, *, request: dict[str, object]) -> object: ...

    def access_secret_version(
        self, *, request: dict[str, object]
    ) -> AccessSecretVersionResponse: ...


class GoogleSecretManagerCredentialStore:
    def __init__(self, project_id: str, client: SecretManagerClient | None = None) -> None:
        self._project_id = project_id
        self._client = client

    async def put(self, connector_id: UUID, authorized_user_json: str) -> str:
        client = await self._get_client()
        parent = f"projects/{self._project_id}"
        secret_id = f"eva-gmail-oauth-{connector_id}"
        secret_name = f"{parent}/secrets/{secret_id}"

        def put_sync() -> bool:
            try:
                try:
                    client.get_secret(request={"name": secret_name})
                except NotFound:
                    try:
                        client.create_secret(
                            request={
                                "parent": parent,
                                "secret_id": secret_id,
                                "secret": {"replication": {"automatic": {}}},
                            }
                        )
                    except AlreadyExists:
                        pass
                client.add_secret_version(
                    request={
                        "parent": secret_name,
                        "payload": {"data": authorized_user_json.encode("utf-8")},
                    }
                )
            except Exception:
                return False
            return True

        if not await asyncio.to_thread(put_sync):
            raise SecretManagerProviderError("Secret Manager credential write failed")
        return secret_name

    async def get(self, secret_reference: str) -> str:
        client = await self._get_client()

        def get_sync() -> str | None:
            try:
                response = client.access_secret_version(
                    request={"name": f"{secret_reference}/versions/latest"}
                )
                return response.payload.data.decode("utf-8")
            except Exception:
                return None

        result = await asyncio.to_thread(get_sync)
        if result is None:
            raise SecretManagerProviderError("Secret Manager credential read failed")
        return result

    async def _get_client(self) -> SecretManagerClient:
        if self._client is None:
            client = await asyncio.to_thread(secretmanager.SecretManagerServiceClient)
            self._client = cast(SecretManagerClient, client)
        return self._client
