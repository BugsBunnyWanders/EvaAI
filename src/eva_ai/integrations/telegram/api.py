from typing import Any, cast

import httpx

from eva_ai.telegram.errors import TelegramProviderError
from eva_ai.telegram.types import TelegramSendResult, TelegramWebhookInfo


class TelegramBotAPI:
    def __init__(
        self,
        token: str,
        *,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def send_message(self, *, chat_id: int, text: str) -> TelegramSendResult:
        result = await self._call("sendMessage", {"chat_id": chat_id, "text": text})
        message_id = result.get("message_id")
        chat = result.get("chat")
        returned_chat_id = chat.get("id") if isinstance(chat, dict) else None
        if not isinstance(message_id, int) or not isinstance(returned_chat_id, int):
            raise TelegramProviderError("INVALID_RESPONSE", retryable=False)
        return TelegramSendResult(message_id=message_id, chat_id=returned_chat_id)

    async def set_webhook(self, *, url: str, secret_token: str) -> None:
        await self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
            },
        )

    async def get_webhook_info(self) -> TelegramWebhookInfo:
        result = await self._call("getWebhookInfo", {})
        return TelegramWebhookInfo(
            url=_string(result.get("url")),
            pending_update_count=_integer(result.get("pending_update_count")),
            last_error_date=_optional_integer(result.get("last_error_date")),
            last_error_message=_optional_string(result.get("last_error_message")),
        )

    async def answer_callback_query(self, *, callback_query_id: str) -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_query_id})

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(self, method: str, payload: dict[str, object]) -> dict[str, Any]:
        try:
            response = await self._client.post(f"{self._base_url}/{method}", json=payload)
        except httpx.TimeoutException, httpx.TransportError:
            raise TelegramProviderError("TRANSPORT_UNAVAILABLE", retryable=True) from None
        try:
            body = cast(dict[str, Any], response.json())
        except ValueError, TypeError:
            raise TelegramProviderError(
                "INVALID_RESPONSE", retryable=response.status_code >= 500
            ) from None
        if response.status_code >= 500 or response.status_code in {408, 429}:
            raise TelegramProviderError("PROVIDER_UNAVAILABLE", retryable=True)
        if response.status_code >= 400 or body.get("ok") is not True:
            error_code = body.get("error_code")
            retryable = isinstance(error_code, int) and (error_code >= 500 or error_code == 429)
            raise TelegramProviderError("REQUEST_REJECTED", retryable=retryable)
        result = body.get("result")
        if result is True:
            return {}
        if not isinstance(result, dict):
            raise TelegramProviderError("INVALID_RESPONSE", retryable=False)
        return result


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TelegramProviderError("INVALID_RESPONSE", retryable=False)
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int):
        raise TelegramProviderError("INVALID_RESPONSE", retryable=False)
    return value


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    return _integer(value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value)[:500]
