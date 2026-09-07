from datetime import UTC, datetime
from typing import cast
from uuid import uuid7

import pytest
from pydantic import ValidationError

from eva_ai.events.service import IngestResult
from eva_ai.events.types import NewEvent
from eva_ai.telegram.ingestion import TelegramEventService
from eva_ai.telegram.repository import TelegramAccountRepository
from eva_ai.telegram.types import (
    PairingConsumeCommand,
    TelegramAccountRecord,
    TelegramAccountStatus,
    TelegramUpdate,
    WebhookDisposition,
)
from eva_ai.telegram.webhook import TelegramWebhookService

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def test_telegram_message_rejects_text_above_conversation_bound() -> None:
    with pytest.raises(ValidationError):
        TelegramUpdate.model_validate(
            {
                "update_id": 1,
                "message": {
                    "message_id": 2,
                    "date": 1,
                    "chat": {"id": 123, "type": "private"},
                    "from": {"id": 123, "first_name": "User"},
                    "text": "x" * 4001,
                },
            }
        )


def _account() -> TelegramAccountRecord:
    return TelegramAccountRecord(
        id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        telegram_user_id=123,
        chat_id=123,
        username="user",
        first_name="User",
        status=TelegramAccountStatus.ACTIVE,
        paired_at=NOW,
        revoked_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


class Accounts:
    def __init__(self, account: TelegramAccountRecord | None) -> None:
        self.account = account
        self.consumed: PairingConsumeCommand | None = None

    async def consume_pairing(self, command: PairingConsumeCommand) -> TelegramAccountRecord:
        self.consumed = command
        assert self.account is not None
        return self.account

    async def find_active(
        self, *, telegram_user_id: int, chat_id: int
    ) -> TelegramAccountRecord | None:
        return self.account


class Events:
    def __init__(self) -> None:
        self.command: NewEvent | None = None

    async def ingest(self, command: NewEvent) -> IngestResult:
        self.command = command
        return IngestResult(event_id=command.id, created=True)


def _update(text: str, *, reply_to: int | None = None) -> TelegramUpdate:
    message: dict[str, object] = {
        "message_id": 9,
        "date": int(NOW.timestamp()),
        "chat": {"id": 123, "type": "private"},
        "from": {"id": 123, "is_bot": False, "first_name": "User", "username": "user"},
        "text": text,
    }
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return TelegramUpdate.model_validate({"update_id": 77, "message": message})


async def test_start_consumes_pairing_without_creating_conversation_event() -> None:
    accounts = Accounts(_account())
    events = Events()
    service = TelegramWebhookService(
        cast(TelegramAccountRepository, accounts), cast(TelegramEventService, events)
    )

    result = await service.handle(_update(f"/start {'x' * 43}"), received_at=NOW)

    assert result.disposition is WebhookDisposition.PAIRED
    assert accounts.consumed is not None
    assert accounts.consumed.telegram_user_id == 123
    assert events.command is None


async def test_paired_message_becomes_user_scoped_event_with_reply_anchor() -> None:
    account = _account()
    events = Events()
    service = TelegramWebhookService(
        cast(TelegramAccountRepository, Accounts(account)), cast(TelegramEventService, events)
    )

    result = await service.handle(_update("Tell me more", reply_to=41), received_at=NOW)

    assert result.disposition is WebhookDisposition.INGESTED
    assert events.command is not None
    assert events.command.user_id == account.user_id
    assert events.command.workspace_id == account.workspace_id
    assert events.command.principal_id == account.id
    assert events.command.payload["text"] == "Tell me more"
    assert events.command.payload["reply_to_message_id"] == 41


async def test_unpaired_and_group_messages_are_safely_ignored() -> None:
    events = Events()
    service = TelegramWebhookService(
        cast(TelegramAccountRepository, Accounts(None)), cast(TelegramEventService, events)
    )

    result = await service.handle(_update("hello"), received_at=NOW)

    assert result.disposition is WebhookDisposition.IGNORED
    assert events.command is None
