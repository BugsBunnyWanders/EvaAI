import asyncio
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
        self.close_failures: list[BaseException] = []
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
        if self.close_failures:
            raise self.close_failures.pop(0)


class BlockingSubscriberClient(FakeSubscriberClient):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = threading.Event()
        self.close_release = threading.Event()
        self.close_finished = threading.Event()

    def close(self) -> None:
        assert threading.get_ident() != self.main_thread_id
        self.calls.append(("close", None))
        self.close_started.set()
        try:
            assert self.close_release.wait(timeout=2)
        finally:
            self.close_finished.set()


@pytest.mark.asyncio
async def test_lazy_subscriber_constructor_failure_is_fixed_and_chain_free() -> None:
    """Fails if Pub/Sub constructor details cross the adapter boundary."""
    marker = "private-subscriber-constructor-response"

    def client_factory() -> FakeSubscriberClient:
        raise RuntimeError(marker)

    subscriber = GooglePullSubscriber(
        "evaai-507018",
        "eva-gmail-ingestion-local",
        client_factory=client_factory,
    )

    with pytest.raises(RuntimeError) as raised:
        await subscriber.pull(10, 5)

    assert type(raised.value).__name__ == "SubscriberProviderError"
    assert str(raised.value) == "Pub/Sub subscriber client construction failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert marker not in repr(raised.value)


@pytest.mark.asyncio
async def test_concurrent_first_subscriber_use_constructs_one_client() -> None:
    """Fails if racing pulls allocate more than one Pub/Sub transport."""
    client = FakeSubscriberClient()
    construction_calls = 0

    def client_factory() -> FakeSubscriberClient:
        nonlocal construction_calls
        construction_calls += 1
        return client

    subscriber = GooglePullSubscriber(
        "evaai-507018",
        "eva-gmail-ingestion-local",
        client_factory=client_factory,
    )

    first, second = await asyncio.gather(subscriber.pull(1, 5), subscriber.pull(1, 5))

    assert first == second
    assert construction_calls == 1
    assert [operation for operation, _ in client.calls].count("subscription_path") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellation_count", [1, 4])
async def test_cancelled_subscriber_construction_keeps_completed_client_owned(
    cancellation_count: int,
) -> None:
    """Fails if repeated cancellation can orphan a completed SubscriberClient."""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    client = FakeSubscriberClient()
    construction_calls = 0

    def client_factory() -> FakeSubscriberClient:
        nonlocal construction_calls
        construction_calls += 1
        started.set()
        try:
            assert release.wait(timeout=2)
            return client
        finally:
            finished.set()

    subscriber = GooglePullSubscriber(
        "evaai-507018",
        "eva-gmail-ingestion-local",
        client_factory=client_factory,
    )
    operation = asyncio.create_task(subscriber.pull(1, 5))
    assert await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=2)
    markers = [object() for _ in range(cancellation_count)]
    accepted: list[bool] = []
    for marker in markers:
        accepted.append(operation.cancel(marker))
        await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await operation

    assert await asyncio.wait_for(asyncio.to_thread(finished.wait), timeout=2)
    assert accepted == [True] * cancellation_count
    assert raised.value.args == (markers[0],)
    assert await subscriber.pull(1, 5)
    assert construction_calls == 1


@pytest.mark.asyncio
async def test_subscriber_close_retries_after_ordinary_failure() -> None:
    """Fails if close clears ownership before the underlying transport succeeds."""
    client = FakeSubscriberClient()
    client.close_failures = [RuntimeError("private-subscriber-close-response")]
    subscriber = GooglePullSubscriber("evaai-507018", "subscription", client)

    with pytest.raises(RuntimeError, match="private-subscriber-close-response"):
        await subscriber.close()
    await subscriber.close()

    assert [operation for operation, _ in client.calls].count("close") == 2


@pytest.mark.asyncio
async def test_subscriber_close_retries_after_cancellation() -> None:
    """Fails if cancelled off-thread cleanup permanently loses retry ownership."""
    client = BlockingSubscriberClient()
    subscriber = GooglePullSubscriber("evaai-507018", "subscription", client)
    close_task = asyncio.create_task(subscriber.close())
    assert await asyncio.wait_for(asyncio.to_thread(client.close_started.wait), timeout=2)
    marker = object()

    close_task.cancel(marker)
    client.close_release.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await close_task
    assert raised.value.args == (marker,)
    assert await asyncio.wait_for(asyncio.to_thread(client.close_finished.wait), timeout=2)

    await subscriber.close()

    assert [operation for operation, _ in client.calls].count("close") == 2


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
async def test_close_is_idempotent_for_worker_and_command_cleanup() -> None:
    """Fails if layered worker/command cleanup closes the Pub/Sub client twice."""
    client = FakeSubscriberClient()
    subscriber = GooglePullSubscriber("evaai-507018", "eva-gmail-ingestion-local", client)

    await subscriber.close()
    await subscriber.close()

    assert client.calls == [
        ("subscription_path", ("evaai-507018", "eva-gmail-ingestion-local")),
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
