import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from fastapi import Request

from eva_ai.actions.executor import ActionExecutor, ActionExecutorStore
from eva_ai.actions.repository import ActionRepository
from eva_ai.config import Settings
from eva_ai.db import Database
from eva_ai.integrations.gcp.secret_manager import GoogleSecretManagerCredentialStore
from eva_ai.integrations.gmail.api import GoogleGmailClientFactory
from eva_ai.telegram.webhook import TelegramWebhookService


def get_database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


def get_telegram_webhook_service(request: Request) -> TelegramWebhookService:
    return cast(TelegramWebhookService, request.app.state.telegram_webhook_service)


@dataclass(slots=True)
class ActionExecutorDependencies:
    database: Database
    credential_store: GoogleSecretManagerCredentialStore
    gmail_client_factory: GoogleGmailClientFactory
    repository: ActionRepository
    executor: ActionExecutor

    async def close(self) -> None:
        interruption: BaseException | None = None
        for close in (
            self.gmail_client_factory.close,
            self.credential_store.close,
            self.database.close,
        ):
            try:
                await close()
            except asyncio.CancelledError as error:
                interruption = interruption or error
            except Exception:
                # Lifespan teardown is best-effort; Cloud Run is already stopping the instance.
                pass
        if interruption is not None:
            raise interruption


def build_action_executor_dependencies(settings: Settings) -> ActionExecutorDependencies:
    if not settings.actions_enabled:
        raise ValueError("action execution is disabled")
    project_id = settings.pubsub_project_id
    if project_id is None or not project_id.strip():
        raise ValueError("Pub/Sub project configuration is incomplete")
    database = Database(settings.database_url.get_secret_value())
    credential_store = GoogleSecretManagerCredentialStore(project_id)
    gmail_client_factory = GoogleGmailClientFactory(
        request_timeout_seconds=settings.gmail_request_timeout_seconds,
        retry_attempts=settings.gmail_retry_attempts,
        retry_initial_backoff_seconds=settings.gmail_retry_initial_backoff_seconds,
        retry_max_backoff_seconds=settings.gmail_retry_max_backoff_seconds,
        retry_jitter_ratio=settings.gmail_retry_jitter_ratio,
    )
    repository = ActionRepository(
        database,
        notification_destination=(
            settings.telegram_delivery_topic_id if settings.telegram_enabled else None
        ),
    )
    executor = ActionExecutor(
        cast(ActionExecutorStore, repository),
        credential_store,
        gmail_client_factory,
        lease_seconds=settings.action_lease_seconds,
        approval_ttl=timedelta(hours=settings.action_approval_ttl_hours),
    )
    return ActionExecutorDependencies(
        database,
        credential_store,
        gmail_client_factory,
        repository,
        executor,
    )
