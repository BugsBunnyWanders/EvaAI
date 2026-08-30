import asyncio
from typing import Protocol, cast
from uuid import UUID

from google.api_core.exceptions import NotFound
from google.cloud import secretmanager


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

        def put_sync() -> None:
            try:
                client.get_secret(request={"name": secret_name})
            except NotFound:
                client.create_secret(
                    request={
                        "parent": parent,
                        "secret_id": secret_id,
                        "secret": {"replication": {"automatic": {}}},
                    }
                )
            client.add_secret_version(
                request={
                    "parent": secret_name,
                    "payload": {"data": authorized_user_json.encode("utf-8")},
                }
            )

        await asyncio.to_thread(put_sync)
        return secret_name

    async def get(self, secret_reference: str) -> str:
        client = await self._get_client()

        def get_sync() -> str:
            response = client.access_secret_version(
                request={"name": f"{secret_reference}/versions/latest"}
            )
            return response.payload.data.decode("utf-8")

        return await asyncio.to_thread(get_sync)

    async def _get_client(self) -> SecretManagerClient:
        if self._client is None:
            client = await asyncio.to_thread(secretmanager.SecretManagerServiceClient)
            self._client = cast(SecretManagerClient, client)
        return self._client
