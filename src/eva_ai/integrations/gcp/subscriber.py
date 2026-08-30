import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from google.api_core.exceptions import DeadlineExceeded
from google.cloud import pubsub_v1

from eva_ai.connectors.gmail.contracts import PullMessage


class ProviderMessage(Protocol):
    data: bytes
    message_id: str


class ReceivedMessage(Protocol):
    ack_id: str
    message: ProviderMessage


class PullResponse(Protocol):
    received_messages: Sequence[ReceivedMessage]


class SubscriberClient(Protocol):
    def subscription_path(self, project: str, subscription: str) -> str: ...

    def pull(self, *, request: dict[str, object], timeout: int) -> PullResponse: ...

    def acknowledge(self, *, request: dict[str, object]) -> None: ...

    def modify_ack_deadline(self, *, request: dict[str, object]) -> None: ...

    def close(self) -> None: ...


class SubscriberProviderError(RuntimeError):
    """Retryable subscriber failure with provider-controlled details removed."""


SubscriberClientFactory = Callable[[], SubscriberClient]


def _default_client_factory() -> SubscriberClient:
    return cast(SubscriberClient, pubsub_v1.SubscriberClient())


@dataclass(frozen=True, slots=True)
class _SubscriberConstruction:
    client: SubscriberClient | None
    subscription_path: str | None
    failed: bool


class GooglePullSubscriber:
    def __init__(
        self,
        project_id: str,
        subscription_id: str,
        client: SubscriberClient | None = None,
        client_factory: SubscriberClientFactory = _default_client_factory,
    ) -> None:
        self._project_id = project_id
        self._subscription_id = subscription_id
        self._client = client
        self._client_factory = client_factory
        self._subscription_path = (
            client.subscription_path(project_id, subscription_id) if client is not None else None
        )
        self._construction_owner: asyncio.Task[_SubscriberConstruction] | None = None

    async def pull(self, max_messages: int, timeout_seconds: int) -> tuple[PullMessage, ...]:
        client, subscription_path = await self._client_and_path()

        def pull_sync() -> PullResponse:
            return client.pull(
                request={
                    "subscription": subscription_path,
                    "max_messages": max_messages,
                },
                timeout=timeout_seconds,
            )

        try:
            response = await asyncio.to_thread(pull_sync)
        except DeadlineExceeded:
            return ()
        return tuple(
            PullMessage(
                ack_id=received.ack_id,
                message_id=received.message.message_id,
                data=received.message.data,
            )
            for received in response.received_messages
        )

    async def acknowledge(self, ack_ids: tuple[str, ...]) -> None:
        client, subscription_path = await self._client_and_path()
        await asyncio.to_thread(
            client.acknowledge,
            request={"subscription": subscription_path, "ack_ids": list(ack_ids)},
        )

    async def negative_acknowledge(self, ack_ids: tuple[str, ...]) -> None:
        client, subscription_path = await self._client_and_path()
        await asyncio.to_thread(
            client.modify_ack_deadline,
            request={
                "subscription": subscription_path,
                "ack_ids": list(ack_ids),
                "ack_deadline_seconds": 0,
            },
        )

    async def close(self) -> None:
        client = self._client
        if client is not None:
            await asyncio.to_thread(client.close)
            if self._client is client:
                self._client = None
                self._subscription_path = None

    async def _client_and_path(self) -> tuple[SubscriberClient, str]:
        if self._client is not None and self._subscription_path is not None:
            return self._client, self._subscription_path

        construction_owner = self._construction_owner
        if construction_owner is None:

            def create_sync() -> _SubscriberConstruction:
                client = self._client
                try:
                    if client is None:
                        client = self._client_factory()
                    path = client.subscription_path(self._project_id, self._subscription_id)
                    if not path:
                        return _SubscriberConstruction(client, None, True)
                    return _SubscriberConstruction(client, path, False)
                except BaseException:
                    # A client created before path failure remains adapter-owned for cleanup.
                    return _SubscriberConstruction(client, None, True)

            async def own_construction() -> _SubscriberConstruction:
                try:
                    return await asyncio.to_thread(create_sync)
                except BaseException:
                    return _SubscriberConstruction(None, None, True)

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
        if result.client is not None:
            self._client = result.client
        if result.subscription_path is not None:
            self._subscription_path = result.subscription_path
        if cancelled is not None:
            raise cancelled
        if result.failed or result.client is None or result.subscription_path is None:
            raise SubscriberProviderError("Pub/Sub subscriber client construction failed")
        return result.client, result.subscription_path
