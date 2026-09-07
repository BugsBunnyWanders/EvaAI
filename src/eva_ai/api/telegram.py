import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError

from eva_ai.api.dependencies import get_telegram_webhook_service
from eva_ai.config import Settings
from eva_ai.telegram.types import TelegramUpdate
from eva_ai.telegram.webhook import TelegramWebhookService

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/telegram")
async def telegram_webhook(
    request: Request,
    service: Annotated[TelegramWebhookService, Depends(get_telegram_webhook_service)],
    secret_header: Annotated[str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = None,
) -> dict[str, bool]:
    settings: Settings = request.app.state.settings
    configured = settings.telegram_webhook_secret
    expected = "" if configured is None else configured.get_secret_value()
    supplied = secret_header or ""
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    body = await request.body()
    if len(body) > settings.telegram_webhook_max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    try:
        update = TelegramUpdate.model_validate_json(body)
    except ValidationError:
        # Telegram can add update variants over time; unsupported shapes are safe no-ops.
        return {"ok": True}
    await service.handle(update)
    return {"ok": True}
