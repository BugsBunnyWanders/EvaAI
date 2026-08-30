import asyncio
from collections.abc import Callable
from enum import Enum, auto
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


class SecretManagerTransport(Protocol):
    def close(self) -> None: ...


class SecretManagerClient(Protocol):
    @property
    def transport(self) -> SecretManagerTransport: ...

    def get_secret(self, *, request: dict[str, object]) -> object: ...

    def create_secret(self, *, request: dict[str, object]) -> object: ...

    def add_secret_version(self, *, request: dict[str, object]) -> object: ...

    def access_secret_version(
        self, *, request: dict[str, object]
    ) -> AccessSecretVersionResponse: ...


class _ClientConstructionFailure(Enum):
    PROVIDER = auto()


SecretManagerClientFactory = Callable[[], SecretManagerClient]


def _default_client_factory() -> SecretManagerClient:
    return cast(SecretManagerClient, secretmanager.SecretManagerServiceClient())


class GoogleSecretManagerCredentialStore:
    def __init__(
        self,
        project_id: str,
        client: SecretManagerClient | None = None,
        client_factory: SecretManagerClientFactory = _default_client_factory,
    ) -> None:
        self._project_id = project_id
        self._client = client
        self._client_factory = client_factory
        self._construction_owner: (
            asyncio.Task[SecretManagerClient | _ClientConstructionFailure] | None
        ) = None

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

    async def close(self) -> None:
        client = self._client
        if client is not None:
            await asyncio.to_thread(client.transport.close)
            if self._client is client:
                self._client = None

    async def _get_client(self) -> SecretManagerClient:
        if self._client is not None:
            return self._client

        construction_owner = self._construction_owner
        if construction_owner is None:

            async def own_construction() -> SecretManagerClient | _ClientConstructionFailure:
                try:
                    return await asyncio.to_thread(self._client_factory)
                except BaseException:
                    return _ClientConstructionFailure.PROVIDER

            construction_owner = asyncio.create_task(own_construction())
            self._construction_owner = construction_owner

        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                result = await asyncio.shield(construction_owner)
                break
            except asyncio.CancelledError as error:
                if cancelled is None:
                    cancelled = error

        if self._construction_owner is construction_owner:
            self._construction_owner = None
        if result is not _ClientConstructionFailure.PROVIDER:
            self._client = result
        if cancelled is not None:
            raise cancelled
        if result is _ClientConstructionFailure.PROVIDER:
            raise SecretManagerProviderError("Secret Manager client construction failed")
        return result
