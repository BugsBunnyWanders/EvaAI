from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from eva_ai.api.health import router as health_router
from eva_ai.config import Settings, get_settings
from eva_ai.db import Database
from eva_ai.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved_settings.database_url.get_secret_value())
        application.state.database = database
        try:
            yield
        finally:
            await database.close()

    application = FastAPI(title=resolved_settings.app_name, lifespan=lifespan)
    application.state.settings = resolved_settings
    application.include_router(health_router)
    return application


app = create_app()
