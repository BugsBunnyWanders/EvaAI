from typing import cast

from fastapi import Request

from eva_ai.db import Database
from eva_ai.telegram.webhook import TelegramWebhookService


def get_database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


def get_telegram_webhook_service(request: Request) -> TelegramWebhookService:
    return cast(TelegramWebhookService, request.app.state.telegram_webhook_service)
