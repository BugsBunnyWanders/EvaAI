from datetime import UTC, datetime

from fastapi.testclient import TestClient
from pydantic import SecretStr

from eva_ai.api.dependencies import get_telegram_webhook_service
from eva_ai.config import Settings
from eva_ai.main import create_app
from eva_ai.telegram.types import TelegramUpdate, WebhookDisposition, WebhookResult


class WebhookService:
    def __init__(self) -> None:
        self.updates: list[TelegramUpdate] = []

    async def handle(
        self, update: TelegramUpdate, *, received_at: datetime | None = None
    ) -> WebhookResult:
        self.updates.append(update)
        return WebhookResult(disposition=WebhookDisposition.INGESTED)


def _payload() -> dict[str, object]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 2,
            "date": int(datetime(2030, 1, 1, tzinfo=UTC).timestamp()),
            "chat": {"id": 3, "type": "private"},
            "from": {"id": 3, "first_name": "User", "is_bot": False},
            "text": "hello",
        },
    }


def test_webhook_requires_exact_secret_and_invokes_service_once() -> None:
    application = create_app(
        Settings(_env_file=None, telegram_webhook_secret=SecretStr("expected-secret"))
    )
    service = WebhookService()
    application.dependency_overrides[get_telegram_webhook_service] = lambda: service

    with TestClient(application) as client:
        unauthorized = client.post("/webhooks/telegram", json=_payload())
        accepted = client.post(
            "/webhooks/telegram",
            json=_payload(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"},
        )

    assert unauthorized.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True}
    assert len(service.updates) == 1


def test_unsupported_update_is_acknowledged_without_service_call() -> None:
    application = create_app(
        Settings(_env_file=None, telegram_webhook_secret=SecretStr("expected-secret"))
    )
    service = WebhookService()
    application.dependency_overrides[get_telegram_webhook_service] = lambda: service

    with TestClient(application) as client:
        response = client.post(
            "/webhooks/telegram",
            json={"update_id": 1, "edited_message": {}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"},
        )

    assert response.status_code == 200
    assert service.updates == []
