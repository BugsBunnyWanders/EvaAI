import asyncio
from collections.abc import Sequence
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


class GooglePullSubscriber:
    def __init__(
        self,
        project_id: str,
        subscription_id: str,
        client: SubscriberClient | None = None,
    ) -> None:
        self._project_id = project_id
        self._subscription_id = subscription_id
        self._client = client
        self._subscription_path = (
            client.subscription_path(project_id, subscription_id) if client is not None else None
        )

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
        client, self._client = self._client, None
        self._subscription_path = None
        if client is not None:
            await asyncio.to_thread(client.close)

    async def _client_and_path(self) -> tuple[SubscriberClient, str]:
        if self._client is None:

            def create_sync() -> tuple[SubscriberClient, str]:
                client = cast(SubscriberClient, pubsub_v1.SubscriberClient())
                return client, client.subscription_path(self._project_id, self._subscription_id)

            self._client, self._subscription_path = await asyncio.to_thread(create_sync)
        assert self._client is not None
        assert self._subscription_path is not None
        return self._client, self._subscription_path
