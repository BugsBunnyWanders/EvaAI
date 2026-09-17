from typing import Protocol

from eva_ai.telegram.types import (
    TelegramInlineKeyboardMarkup,
    TelegramSendResult,
    TelegramWebhookInfo,
)


class TelegramGateway(Protocol):
    async def send_chat_action(self, *, chat_id: int, action: str) -> None: ...

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: TelegramInlineKeyboardMarkup | None = None,
        preserve_text: bool = False,
    ) -> TelegramSendResult: ...

    async def set_webhook(self, *, url: str, secret_token: str) -> None: ...

    async def get_webhook_info(self) -> TelegramWebhookInfo: ...

    async def answer_callback_query(self, *, callback_query_id: str) -> None: ...
