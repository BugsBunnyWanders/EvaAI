import json

import httpx
import pytest

from eva_ai.integrations.telegram.api import TelegramBotAPI
from eva_ai.telegram.errors import TelegramProviderError


async def test_send_message_uses_scoped_chat_and_returns_provider_anchor() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 17, "chat": {"id": 42}}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = TelegramBotAPI("secret-token", client=client)

    result = await api.send_message(chat_id=42, text="hello")

    assert result.message_id == 17
    assert result.chat_id == 42
    assert json.loads(requests[0].content) == {"chat_id": 42, "text": "hello"}
    await client.aclose()


async def test_provider_server_failure_is_sanitized_and_retryable() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json={"ok": False, "description": "secret"})
        )
    )
    api = TelegramBotAPI("secret-token", client=client)

    with pytest.raises(TelegramProviderError) as raised:
        await api.send_message(chat_id=42, text="private text")

    assert raised.value.retryable is True
    assert "secret" not in str(raised.value)
    assert "private text" not in str(raised.value)
    await client.aclose()
