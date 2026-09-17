from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol, cast

from fastapi import APIRouter, FastAPI, Request, Response, status

from eva_ai.actions.types import (
    ActionExecutionOutcome,
    ActionExecutionResult,
    ActionTaskRequest,
)
from eva_ai.api.dependencies import build_action_executor_dependencies
from eva_ai.config import Settings, get_settings


class ActionRequestExecutor(Protocol):
    async def execute(self, request: ActionTaskRequest) -> ActionExecutionResult: ...


def create_action_executor_app(executor: ActionRequestExecutor) -> FastAPI:
    application = FastAPI(title="Eva private action executor")
    router = APIRouter()

    @router.post("/internal/actions/execute", response_model=ActionExecutionResult)
    async def execute_action(
        request: ActionTaskRequest,
        response: Response,
    ) -> ActionExecutionResult:
        result = await executor.execute(request)
        if result.outcome is ActionExecutionOutcome.RETRY:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return result

    application.include_router(router)
    return application


def create_action_executor_runtime_app(settings: Settings | None = None) -> FastAPI:
    """Build the private Cloud Run app without importing Eva's public API routes."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        dependencies = build_action_executor_dependencies(settings or get_settings())
        application.state.action_executor_dependencies = dependencies
        application.state.action_executor = dependencies.executor
        try:
            yield
        finally:
            await dependencies.close()

    application = FastAPI(title="Eva private action executor", lifespan=lifespan)
    router = APIRouter()

    @router.post("/internal/actions/execute", response_model=ActionExecutionResult)
    async def execute_action(
        action_request: ActionTaskRequest,
        request: Request,
        response: Response,
    ) -> ActionExecutionResult:
        executor = cast(ActionRequestExecutor, request.app.state.action_executor)
        result = await executor.execute(action_request)
        if result.outcome is ActionExecutionOutcome.RETRY:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return result

    application.include_router(router)
    return application


app = create_action_executor_runtime_app()
