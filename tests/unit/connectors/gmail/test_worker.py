import asyncio
import io
import json
import logging
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from eva_ai.config import LogFormat, Settings
from eva_ai.connectors.gmail.contracts import GmailNotification, PullMessage, PullSubscriber
from eva_ai.connectors.gmail.maintenance import GmailMaintenanceService, MaintenanceSummary
from eva_ai.connectors.gmail.notification import decode_notification
from eva_ai.connectors.gmail.sync import (
    GmailSyncError,
    GmailSyncService,
    SyncResult,
    SyncStatus,
)
from eva_ai.connectors.gmail.worker import (
    GmailPullWorker,
    GmailWorkerMaintenanceError,
    GmailWorkerTransportError,
    PullBatchResult,
)
from eva_ai.integrations.gcp.secret_manager import SecretManagerProviderError
from eva_ai.integrations.gmail.api import GmailProviderError
from eva_ai.logging import configure_logging

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)
CONNECTOR_ID = UUID("0191cafe-7b00-7000-8000-000000000003")


@pytest.fixture(autouse=True)
def enable_worker_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging.getLogger("eva_ai.connectors.gmail.worker"), "disabled", False)


def message(index: int, account: str | None = None) -> PullMessage:
    mailbox = account or f"owner-{index}@example.com"
    return PullMessage(
        ack_id=f"ack-{index}",
        message_id=f"pubsub-{index}",
        data=(f'{{"emailAddress":"{mailbox}","historyId":"{100 + index}"}}').encode(),
    )


class FakeSubscriber:
    def __init__(self, messages: tuple[PullMessage, ...] = ()) -> None:
        self.messages = messages
        self.calls: list[tuple[str, object]] = []
        self.pull_failure: BaseException | None = None
        self.ack_failure: BaseException | None = None
        self.nack_failure: BaseException | None = None
        self.close_failure: BaseException | None = None

    async def pull(self, max_messages: int, timeout_seconds: int) -> tuple[PullMessage, ...]:
        self.calls.append(("pull", (max_messages, timeout_seconds)))
        if self.pull_failure is not None:
            raise self.pull_failure
        return self.messages

    async def acknowledge(self, ack_ids: tuple[str, ...]) -> None:
        self.calls.append(("acknowledge", ack_ids))
        if self.ack_failure is not None:
            raise self.ack_failure

    async def negative_acknowledge(self, ack_ids: tuple[str, ...]) -> None:
        self.calls.append(("negative_acknowledge", ack_ids))
        if self.nack_failure is not None:
            raise self.nack_failure

    async def close(self) -> None:
        self.calls.append(("close", None))
        if self.close_failure is not None:
            raise self.close_failure


class FakeSyncService:
    def __init__(self) -> None:
        self.outcomes: dict[str, SyncResult | BaseException] = {}
        self.notifications: list[GmailNotification] = []

    async def handle(self, notification: GmailNotification) -> SyncResult:
        self.notifications.append(notification)
        outcome = self.outcomes[notification.email_address]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeMaintenanceService:
    def __init__(self) -> None:
        self.calls: list[datetime] = []
        self.summary = MaintenanceSummary(renewed=0, safety_synced=0, failed=0)
        self.failure: BaseException | None = None

    async def run_due(self, now: datetime) -> MaintenanceSummary:
        self.calls.append(now)
        if self.failure is not None:
            raise self.failure
        return self.summary


class Harness:
    def __init__(self, messages: tuple[PullMessage, ...] = ()) -> None:
        self.subscriber = FakeSubscriber(messages)
        self.sync = FakeSyncService()
        self.maintenance = FakeMaintenanceService()
        self.worker = GmailPullWorker(
            subscriber=cast(PullSubscriber, self.subscriber),
            sync_service=cast(GmailSyncService, self.sync),
            maintenance=cast(GmailMaintenanceService, self.maintenance),
            clock=lambda: NOW,
            pull_timeout_seconds=17,
        )


async def test_run_once_applies_ack_matrix_and_isolates_each_message() -> None:
    """Fails on any wrong ACK branch, skipped later message, or repeated group call."""
    messages = tuple(message(index) for index in range(1, 11)) + (
        PullMessage(
            ack_id="ack-11",
            message_id="pubsub-11",
            data=b'{"emailAddress":"raw-private@example.com","historyId":"invalid"}',
        ),
    )
    harness = Harness(messages)
    statuses = (
        SyncStatus.SYNCED,
        SyncStatus.ALREADY_COVERED,
        SyncStatus.UNKNOWN_ACCOUNT,
        SyncStatus.REAUTHORIZATION_REQUIRED,
        SyncStatus.CONNECTING,
        SyncStatus.BUSY,
    )
    for index, status in enumerate(statuses, 1):
        harness.sync.outcomes[f"owner-{index}@example.com"] = SyncResult(
            status,
            None if status is SyncStatus.UNKNOWN_ACCOUNT else CONNECTOR_ID,
            0,
            None,
        )
    harness.sync.outcomes["owner-7@example.com"] = GmailProviderError("provider-response-private")
    harness.sync.outcomes["owner-8@example.com"] = SecretManagerProviderError(
        "refresh-token-private"
    )
    harness.sync.outcomes["owner-9@example.com"] = GmailSyncError("database-private")
    harness.sync.outcomes["owner-10@example.com"] = RuntimeError(
        "subject recipient body snippet token provider response"
    )

    result = await harness.worker.run_once(max_messages=11)

    assert result == PullBatchResult(pulled=11, acknowledged=5, negative_acknowledged=6)
    assert [notification.email_address for notification in harness.sync.notifications] == [
        f"owner-{index}@example.com" for index in range(1, 11)
    ]
    assert harness.subscriber.calls == [
        ("pull", (11, 17)),
        ("acknowledge", ("ack-1", "ack-2", "ack-3", "ack-4", "ack-11")),
        (
            "negative_acknowledge",
            ("ack-5", "ack-6", "ack-7", "ack-8", "ack-9", "ack-10"),
        ),
    ]
    assert harness.maintenance.calls == [NOW]


async def test_unexpected_decoder_failure_nacks_only_that_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails if an internal decode fault prevents later pulled messages being classified."""
    harness = Harness((message(1), message(2)))
    harness.sync.outcomes["owner-2@example.com"] = SyncResult(
        SyncStatus.SYNCED, CONNECTOR_ID, 1, "102"
    )
    calls = 0

    def fail_once(data: bytes) -> GmailNotification:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("decoder-private-raw-notification")
        return decode_notification(data)

    monkeypatch.setattr("eva_ai.connectors.gmail.worker.decode_notification", fail_once)

    result = await harness.worker.run_once(max_messages=2)

    assert result == PullBatchResult(pulled=2, acknowledged=1, negative_acknowledged=1)
    assert harness.subscriber.calls[1:] == [
        ("acknowledge", ("ack-2",)),
        ("negative_acknowledge", ("ack-1",)),
    ]


async def test_empty_deadline_batch_still_runs_persisted_maintenance() -> None:
    """Fails if maintenance wake-ups depend on receiving a notification."""
    harness = Harness()

    result = await harness.worker.run_once()

    assert result == PullBatchResult(pulled=0, acknowledged=0, negative_acknowledged=0)
    assert harness.subscriber.calls == [("pull", (10, 17))]
    assert harness.maintenance.calls == [NOW]


async def test_maintenance_summary_failure_is_visible_without_changing_ack_decisions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fails if due-work failures are silent or rewrite notification durability decisions."""
    harness = Harness((message(1),))
    harness.sync.outcomes["owner-1@example.com"] = SyncResult(
        SyncStatus.SYNCED, CONNECTOR_ID, 1, "101"
    )
    harness.maintenance.summary = MaintenanceSummary(renewed=0, safety_synced=0, failed=2)

    result = await harness.worker.run_once()

    assert result == PullBatchResult(pulled=1, acknowledged=1, negative_acknowledged=0)
    assert harness.subscriber.calls[-1] == ("acknowledge", ("ack-1",))
    assert any(
        record.__dict__.get("error_category") == "maintenance_failed" for record in caplog.records
    )


async def test_maintenance_exception_is_chain_free_after_ack_decisions() -> None:
    """Fails if a maintenance error alters ACKs or exposes its exception text/chain."""
    harness = Harness((message(1),))
    harness.sync.outcomes["owner-1@example.com"] = SyncResult(
        SyncStatus.SYNCED, CONNECTOR_ID, 1, "101"
    )
    harness.maintenance.failure = RuntimeError("maintenance-private-provider-response")

    with pytest.raises(GmailWorkerMaintenanceError) as raised:
        await harness.worker.run_once()

    assert str(raised.value) == "Gmail maintenance failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "maintenance-private-provider-response" not in repr(raised.value)
    assert ("acknowledge", ("ack-1",)) in harness.subscriber.calls


async def test_transport_and_maintenance_failures_are_both_operationally_visible(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fails if a transport error hides a separate post-batch maintenance failure."""
    harness = Harness((message(1),))
    harness.sync.outcomes["owner-1@example.com"] = SyncResult(
        SyncStatus.SYNCED, CONNECTOR_ID, 1, "101"
    )
    harness.subscriber.ack_failure = RuntimeError("ack-private-provider-response")
    harness.maintenance.failure = RuntimeError("maintenance-private-provider-response")

    with pytest.raises(GmailWorkerTransportError):
        await harness.worker.run_once()

    assert {
        record.__dict__.get("error_category")
        for record in caplog.records
        if record.name == "eva_ai.connectors.gmail.worker"
    } >= {"transport_failed", "maintenance_failed"}


@pytest.mark.parametrize("failed_operation", ["ack", "nack"])
async def test_acknowledgement_failure_is_safe_and_other_group_is_attempted(
    failed_operation: str,
) -> None:
    """Fails if one group failure skips the other or leaks transport exception content."""
    harness = Harness((message(1), message(2)))
    harness.sync.outcomes = {
        "owner-1@example.com": SyncResult(SyncStatus.SYNCED, CONNECTOR_ID, 1, "101"),
        "owner-2@example.com": SyncResult(SyncStatus.BUSY, CONNECTOR_ID, 0, "100"),
    }
    marker = f"{failed_operation}-private-token-provider-response"
    if failed_operation == "ack":
        harness.subscriber.ack_failure = RuntimeError(marker)
    else:
        harness.subscriber.nack_failure = RuntimeError(marker)

    with pytest.raises(GmailWorkerTransportError) as raised:
        await harness.worker.run_once()

    assert str(raised.value) == "Gmail acknowledgement transport failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert marker not in repr(raised.value)
    assert harness.subscriber.calls[1:] == [
        ("acknowledge", ("ack-1",)),
        ("negative_acknowledge", ("ack-2",)),
    ]
    assert harness.maintenance.calls == [NOW]


async def test_failure_logs_emit_only_fixed_text_and_allowlisted_context() -> None:
    """Fails if raw notification, mailbox, content, tokens, or exception text reaches logs."""
    stream = io.StringIO()
    configure_logging(
        Settings(log_level="INFO", log_format=LogFormat.JSON, _env_file=None), stream=stream
    )
    sensitive = {
        "mailbox": "recipient-private@example.com",
        "subject": "subject-private",
        "recipient": "to-private@example.com",
        "body": "body-private",
        "snippet": "snippet-private",
        "token": "refresh-token-private",
        "provider": "provider-response-private",
    }
    malformed = PullMessage(
        ack_id="ack-malformed",
        message_id="pubsub-malformed",
        data=json.dumps(sensitive | {"emailAddress": sensitive["mailbox"]}).encode(),
    )
    unexpected = message(2, sensitive["mailbox"])
    harness = Harness((malformed, unexpected))
    harness.sync.outcomes[sensitive["mailbox"]] = RuntimeError(" ".join(sensitive.values()))

    await harness.worker.run_once()

    rendered = stream.getvalue()
    records = [json.loads(line) for line in rendered.splitlines()]
    assert records
    for record in records:
        assert set(record) <= {
            "timestamp",
            "severity",
            "logger",
            "message",
            "connector_id",
            "workspace_id",
            "pubsub_message_id",
            "gmail_message_id",
            "gmail_thread_id",
            "claim_id",
            "operation",
            "outcome",
            "error_category",
        }
    assert {record.get("pubsub_message_id") for record in records} == {
        "pubsub-malformed",
        "pubsub-2",
    }
    for marker in sensitive.values():
        assert marker not in rendered
    assert malformed.data.decode() not in rendered


async def test_run_forever_propagates_cancellation_after_close_without_sleep() -> None:
    """Fails if cancellation is swallowed, cleanup is skipped, or a sleep owns wake-ups."""
    harness = Harness()
    cancellation = asyncio.CancelledError("worker-cancelled")
    harness.subscriber.pull_failure = cancellation

    with pytest.raises(asyncio.CancelledError) as raised:
        await harness.worker.run_forever()

    assert raised.value is cancellation
    assert harness.subscriber.calls == [("pull", (10, 17)), ("close", None)]


async def test_close_failure_is_chain_free_and_does_not_mask_cancellation() -> None:
    """Fails if shutdown exposes close text or replaces the triggering cancellation."""
    harness = Harness()
    cancellation = asyncio.CancelledError("worker-cancelled")
    harness.subscriber.pull_failure = cancellation
    harness.subscriber.close_failure = RuntimeError("close-private-token")

    with pytest.raises(asyncio.CancelledError) as raised:
        await harness.worker.run_forever()

    assert raised.value is cancellation
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "close-private-token" not in repr(raised.value)


async def test_close_failure_without_primary_error_is_content_free() -> None:
    """Fails if subscriber cleanup errors escape with provider-controlled content."""
    harness = Harness()
    harness.subscriber.pull_failure = GmailWorkerTransportError("Gmail pull failed")
    harness.subscriber.close_failure = RuntimeError("close-private-token")

    with pytest.raises(GmailWorkerTransportError) as raised:
        await harness.worker.run_forever()

    assert str(raised.value) == "Gmail pull failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "close-private-token" not in repr(raised.value)
