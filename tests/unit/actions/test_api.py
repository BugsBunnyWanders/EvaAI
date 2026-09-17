from uuid import uuid7

import httpx
import pytest

from eva_ai.actions.api import create_action_executor_app, create_action_executor_runtime_app
from eva_ai.actions.types import (
    ActionExecutionOutcome,
    ActionExecutionResult,
    ActionTaskRequest,
)
from eva_ai.config import Settings


class Executor:
    def __init__(self, outcome: ActionExecutionOutcome) -> None:
        self.outcome = outcome
        self.requests: list[ActionTaskRequest] = []

    async def execute(self, request: ActionTaskRequest) -> ActionExecutionResult:
        self.requests.append(request)
        return ActionExecutionResult(action_id=request.action_id, outcome=self.outcome)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,status_code",
    [
        (ActionExecutionOutcome.SUCCEEDED, 200),
        (ActionExecutionOutcome.TERMINAL, 200),
        (ActionExecutionOutcome.UNKNOWN, 200),
        (ActionExecutionOutcome.UNAVAILABLE, 200),
        (ActionExecutionOutcome.RETRY, 503),
    ],
)
async def test_private_executor_route_maps_only_safe_retry_to_non_2xx(
    outcome: ActionExecutionOutcome,
    status_code: int,
) -> None:
    executor = Executor(outcome)
    app = create_action_executor_app(executor)
    request = ActionTaskRequest(action_id=uuid7())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://executor.test",
    ) as client:
        response = await client.post(
            "/internal/actions/execute",
            json=request.model_dump(mode="json"),
        )

    assert response.status_code == status_code
    assert response.json() == {
        "action_id": str(request.action_id),
        "outcome": outcome,
        "provider_status_category": None,
    }
    assert executor.requests == [request]


def test_runtime_executor_app_exposes_no_public_or_telegram_routes() -> None:
    app = create_action_executor_runtime_app(Settings(_env_file=None))

    paths = set(app.openapi()["paths"])
    assert "/internal/actions/execute" in paths
    assert not any(path.startswith("/telegram") for path in paths)


@pytest.mark.asyncio
async def test_public_application_does_not_expose_private_action_route() -> None:
    from eva_ai.main import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="https://public.test",
    ) as client:
        response = await client.post(
            "/internal/actions/execute",
            json=ActionTaskRequest(action_id=uuid7()).model_dump(mode="json"),
        )

    assert response.status_code == 404
