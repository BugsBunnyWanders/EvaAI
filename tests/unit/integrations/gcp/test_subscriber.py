import threading
from types import SimpleNamespace
from typing import cast

import pytest
from google.api_core.exceptions import DeadlineExceeded

from eva_ai.connectors.gmail.contracts import PullMessage
from eva_ai.integrations.gcp.subscriber import GooglePullSubscriber, PullResponse


class FakeSubscriberClient:
    def __init__(self) -> None:
        self.main_thread_id = threading.get_ident()
        self.calls: list[tuple[str, object]] = []
        self.pull_error: Exception | None = None
        self.pull_response = SimpleNamespace(
            received_messages=[
                SimpleNamespace(
                    ack_id="ack-1",
                    message=SimpleNamespace(
                        data=b'{"emailAddress":"owner@example.com","historyId":"99"}',
                        message_id="provider-message-1",
                        attributes={"source": "gmail"},
                        publish_time=None,
                        ordering_key="",
                    ),
                    delivery_attempt=1,
                )
            ]
        )

    def subscription_path(self, project: str, subscription: str) -> str:
        self.calls.append(("subscription_path", (project, subscription)))
        return f"projects/{project}/subscriptions/{subscription}"

    def pull(self, *, request: dict[str, object], timeout: int) -> PullResponse:
        assert threading.get_ident() != self.main_thread_id
        self.calls.append(("pull", (request, timeout)))
        if self.pull_error is not None:
            raise self.pull_error
        return cast(PullResponse, self.pull_response)

    def acknowledge(self, *, request: dict[str, object]) -> None:
        assert threading.get_ident() != self.main_thread_id
        self.calls.append(("acknowledge", request))

    def modify_ack_deadline(self, *, request: dict[str, object]) -> None:
        assert threading.get_ident() != self.main_thread_id
        self.calls.append(("modify_ack_deadline", request))

    def close(self) -> None:
        assert threading.get_ident() != self.main_thread_id
        self.calls.append(("close", None))


@pytest.mark.asyncio
async def test_pull_uses_exact_subscription_and_returns_client_decoded_bytes() -> None:
    """Fails on a wrong pull path/request or accidental second base64 decode."""
    client = FakeSubscriberClient()
    subscriber = GooglePullSubscriber("evaai-507018", "eva-gmail-ingestion-local", client)

    messages = await subscriber.pull(max_messages=25, timeout_seconds=30)

    subscription = "projects/evaai-507018/subscriptions/eva-gmail-ingestion-local"
    assert messages == (
        PullMessage(
            ack_id="ack-1",
            message_id="provider-message-1",
            data=b'{"emailAddress":"owner@example.com","historyId":"99"}',
        ),
    )
    assert client.calls == [
        ("subscription_path", ("evaai-507018", "eva-gmail-ingestion-local")),
        (
            "pull",
            ({"subscription": subscription, "max_messages": 25}, 30),
        ),
    ]


@pytest.mark.asyncio
async def test_acknowledge_and_negative_acknowledge_map_ack_ids_exactly() -> None:
    """Fails if ack IDs are changed or nack does not request immediate redelivery."""
    client = FakeSubscriberClient()
    subscriber = GooglePullSubscriber("evaai-507018", "eva-gmail-ingestion-local", client)
    subscription = "projects/evaai-507018/subscriptions/eva-gmail-ingestion-local"

    await subscriber.acknowledge(("ack-1", "ack-2"))
    await subscriber.negative_acknowledge(("ack-3",))
    await subscriber.close()

    assert client.calls == [
        ("subscription_path", ("evaai-507018", "eva-gmail-ingestion-local")),
        (
            "acknowledge",
            {"subscription": subscription, "ack_ids": ["ack-1", "ack-2"]},
        ),
        (
            "modify_ack_deadline",
            {
                "subscription": subscription,
                "ack_ids": ["ack-3"],
                "ack_deadline_seconds": 0,
            },
        ),
        ("close", None),
    ]


@pytest.mark.asyncio
async def test_pull_deadline_returns_empty_but_other_failures_propagate() -> None:
    """Fails if an ordinary long poll timeout is retried or real failures are swallowed."""
    client = FakeSubscriberClient()
    subscriber = GooglePullSubscriber("evaai-507018", "eva-gmail-ingestion-local", client)
    client.pull_error = DeadlineExceeded(  # type: ignore[no-untyped-call]
        "ordinary long-poll deadline"
    )

    assert await subscriber.pull(10, 5) == ()

    client.pull_error = RuntimeError("transport unavailable")
    with pytest.raises(RuntimeError, match="^transport unavailable$"):
        await subscriber.pull(10, 5)
