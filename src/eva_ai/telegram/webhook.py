from datetime import UTC, datetime

from pydantic import ValidationError

from eva_ai.events.types import NewEvent, PrincipalType
from eva_ai.telegram.errors import PairingCodeInvalidError, TelegramAccountConflictError
from eva_ai.telegram.ingestion import TelegramEventService
from eva_ai.telegram.repository import TelegramAccountRepository
from eva_ai.telegram.types import (
    PairingConsumeCommand,
    TelegramCallbackQuery,
    TelegramMessage,
    TelegramUpdate,
    WebhookDisposition,
    WebhookResult,
)


class TelegramWebhookService:
    def __init__(
        self,
        accounts: TelegramAccountRepository,
        events: TelegramEventService,
    ) -> None:
        self._accounts = accounts
        self._events = events

    async def handle(
        self, update: TelegramUpdate, *, received_at: datetime | None = None
    ) -> WebhookResult:
        effective_received_at = received_at or datetime.now(UTC)
        if update.message is not None:
            return await self._handle_message(update, update.message, effective_received_at)
        if update.callback_query is not None:
            return await self._handle_callback(update, update.callback_query, effective_received_at)
        return WebhookResult(disposition=WebhookDisposition.IGNORED)

    async def _handle_message(
        self,
        update: TelegramUpdate,
        message: TelegramMessage,
        received_at: datetime,
    ) -> WebhookResult:
        sender = message.from_user
        if (
            message.chat.type != "private"
            or sender is None
            or sender.is_bot
            or message.text is None
        ):
            return WebhookResult(disposition=WebhookDisposition.IGNORED)

        start_token = _start_token(message.text)
        if start_token is not None:
            try:
                await self._accounts.consume_pairing(
                    PairingConsumeCommand(
                        token=start_token,
                        telegram_user_id=sender.id,
                        chat_id=message.chat.id,
                        username=sender.username,
                        first_name=sender.first_name,
                        consumed_at=received_at,
                    )
                )
            except PairingCodeInvalidError, TelegramAccountConflictError, ValidationError:
                # Pairing failures are intentionally indistinguishable to untrusted chat users.
                return WebhookResult(disposition=WebhookDisposition.IGNORED)
            return WebhookResult(disposition=WebhookDisposition.PAIRED)

        account = await self._accounts.find_active(
            telegram_user_id=sender.id, chat_id=message.chat.id
        )
        if account is None:
            return WebhookResult(disposition=WebhookDisposition.IGNORED)
        command = "new" if _is_new_command(message.text) else None
        event = NewEvent(
            user_id=account.user_id,
            workspace_id=account.workspace_id,
            source="TELEGRAM",
            event_type="telegram.message.received",
            external_id=str(update.update_id),
            idempotency_key=f"telegram:update:{update.update_id}",
            occurred_at=datetime.fromtimestamp(message.date, tz=UTC),
            received_at=received_at,
            principal_type=PrincipalType.USER,
            principal_id=account.id,
            actor={"telegram_user_id": sender.id},
            subject={"chat_id": message.chat.id},
            payload={
                "update_id": update.update_id,
                "message_id": message.message_id,
                "chat_id": message.chat.id,
                "telegram_user_id": sender.id,
                "text": message.text,
                "reply_to_message_id": (
                    message.reply_to_message.message_id
                    if message.reply_to_message is not None
                    else None
                ),
                "command": command,
            },
            metadata={},
            correlation_keys=[f"telegram:chat:{message.chat.id}"],
        )
        ingested = await self._events.ingest(event)
        return WebhookResult(
            disposition=(
                WebhookDisposition.INGESTED if ingested.created else WebhookDisposition.DUPLICATE
            ),
            event_id=ingested.event_id,
        )

    async def _handle_callback(
        self,
        update: TelegramUpdate,
        callback: TelegramCallbackQuery,
        received_at: datetime,
    ) -> WebhookResult:
        message = callback.message
        if message is None or message.chat.type != "private":
            return WebhookResult(disposition=WebhookDisposition.IGNORED)
        account = await self._accounts.find_active(
            telegram_user_id=callback.from_user.id, chat_id=message.chat.id
        )
        if account is None:
            return WebhookResult(disposition=WebhookDisposition.IGNORED)
        event = NewEvent(
            user_id=account.user_id,
            workspace_id=account.workspace_id,
            source="TELEGRAM",
            event_type="telegram.callback.received",
            external_id=str(update.update_id),
            idempotency_key=f"telegram:update:{update.update_id}",
            occurred_at=received_at,
            received_at=received_at,
            principal_type=PrincipalType.USER,
            principal_id=account.id,
            actor={"telegram_user_id": callback.from_user.id},
            subject={"chat_id": message.chat.id},
            payload={
                "update_id": update.update_id,
                "callback_query_id": callback.id,
                "message_id": message.message_id,
                "chat_id": message.chat.id,
                "telegram_user_id": callback.from_user.id,
                "data": callback.data,
            },
            metadata={},
            correlation_keys=[f"telegram:chat:{message.chat.id}"],
        )
        ingested = await self._events.ingest(event)
        return WebhookResult(
            disposition=(
                WebhookDisposition.INGESTED if ingested.created else WebhookDisposition.DUPLICATE
            ),
            event_id=ingested.event_id,
        )


def _start_token(text: str) -> str | None:
    parts = text.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0].split("@", maxsplit=1)[0].casefold() != "/start":
        return None
    token = parts[1].strip()
    return token or None


def _is_new_command(text: str) -> bool:
    return text.strip().split(maxsplit=1)[0].split("@", maxsplit=1)[0].casefold() == "/new"
