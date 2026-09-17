from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from eva_ai.actions.repository import ActionRepository
from eva_ai.actions.service import ActionApprovalService
from eva_ai.api.health import router as health_router
from eva_ai.api.telegram import router as telegram_router
from eva_ai.config import Settings, get_settings
from eva_ai.db import Database
from eva_ai.integrations.telegram.api import TelegramBotAPI
from eva_ai.logging import configure_logging
from eva_ai.telegram.ingestion import TelegramEventService
from eva_ai.telegram.repository import TelegramAccountRepository
from eva_ai.telegram.webhook import TelegramWebhookService


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved_settings.database_url.get_secret_value())
        application.state.database = database
        approvals = None
        telegram = None
        if resolved_settings.action_approval_enabled:
            approvals = ActionApprovalService(
                ActionRepository(
                    database,
                    notification_destination=resolved_settings.telegram_delivery_topic_id,
                ),
                destination=resolved_settings.pubsub_topic_id,
                revision_ttl=timedelta(seconds=resolved_settings.action_revision_ttl_seconds),
            )
            token = resolved_settings.telegram_bot_token
            if token is not None and token.get_secret_value().strip():
                telegram = TelegramBotAPI(
                    token.get_secret_value(),
                    timeout_seconds=resolved_settings.gmail_request_timeout_seconds,
                )
        application.state.telegram_webhook_service = TelegramWebhookService(
            TelegramAccountRepository(database),
            TelegramEventService(database, resolved_settings.telegram_turn_topic_id),
            approvals=approvals,
            telegram=telegram,
        )
        try:
            yield
        finally:
            if telegram is not None:
                await telegram.close()
            await database.close()

    application = FastAPI(title=resolved_settings.app_name, lifespan=lifespan)
    application.state.settings = resolved_settings
    application.include_router(health_router)
    application.include_router(telegram_router)
    return application


app = create_app()
