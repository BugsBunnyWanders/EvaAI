from typing import Protocol

from fastapi import APIRouter, FastAPI, Response, status

from eva_ai.actions.types import (
    ActionExecutionOutcome,
    ActionExecutionResult,
    ActionTaskRequest,
)


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
