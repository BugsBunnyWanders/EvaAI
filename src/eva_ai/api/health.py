import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from eva_ai.api.dependencies import get_database
from eva_ai.db import Database

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", response_model=None)
async def readiness(
    database: Annotated[Database, Depends(get_database)],
) -> dict[str, str] | JSONResponse:
    try:
        await database.ping()
    except Exception as error:
        logger.warning("Database readiness check failed: %s", type(error).__name__)
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return {"status": "ready"}
