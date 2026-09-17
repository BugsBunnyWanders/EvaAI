import hashlib
import json
from typing import Any
from uuid import uuid7

import pytest
from google.api_core.exceptions import AlreadyExists, ServiceUnavailable

from eva_ai.actions.types import ActionTaskRequest, GmailActionCapability
from eva_ai.integrations.gcp.tasks import GoogleCloudTaskEnqueuer


class Client:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def queue_path(self, project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def task_path(self, project: str, location: str, queue: str, task: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}/tasks/{task}"

    async def create_task(self, request: dict[str, Any]) -> object:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return object()

    async def close(self) -> None:
        pass


def _enqueuer(client: Client) -> GoogleCloudTaskEnqueuer:
    return GoogleCloudTaskEnqueuer(
        project_id="eva-project",
        location="asia-south1",
        queue_id="eva-actions",
        executor_url="https://executor.example/internal/actions/execute",
        service_account_email="eva-task-caller@eva-project.iam.gserviceaccount.com",
        audience="https://executor.example",
        client=client,
    )


@pytest.mark.asyncio
async def test_enqueue_builds_opaque_authenticated_http_task() -> None:
    client = Client()
    enqueuer = _enqueuer(client)
    request = ActionTaskRequest(action_id=uuid7())

    task_name = await enqueuer.enqueue(request, GmailActionCapability.CREATE_DRAFT)

    expected_id = hashlib.sha256(
        f"{request.action_id}:{GmailActionCapability.CREATE_DRAFT}".encode()
    ).hexdigest()
    expected_name = (
        f"projects/eva-project/locations/asia-south1/queues/eva-actions/tasks/action-{expected_id}"
    )
    assert task_name == expected_name
    assert client.requests == [
        {
            "parent": "projects/eva-project/locations/asia-south1/queues/eva-actions",
            "task": {
                "name": expected_name,
                "http_request": {
                    "http_method": 1,
                    "url": "https://executor.example/internal/actions/execute",
                    "headers": {"Content-Type": "application/json"},
                    "body": request.model_dump_json().encode(),
                    "oidc_token": {
                        "service_account_email": (
                            "eva-task-caller@eva-project.iam.gserviceaccount.com"
                        ),
                        "audience": "https://executor.example",
                    },
                },
            },
        }
    ]
    assert json.loads(client.requests[0]["task"]["http_request"]["body"]) == {
        "action_id": str(request.action_id),
        "schema_version": 1,
    }


@pytest.mark.asyncio
async def test_task_name_changes_with_capability_and_is_stable_for_replay() -> None:
    client = Client()
    enqueuer = _enqueuer(client)
    request = ActionTaskRequest(action_id=uuid7())

    first = await enqueuer.enqueue(request, GmailActionCapability.CREATE_DRAFT)
    replay = await enqueuer.enqueue(request, GmailActionCapability.CREATE_DRAFT)
    different = await enqueuer.enqueue(request, GmailActionCapability.UPDATE_DRAFT)

    assert first == replay
    assert first != different


@pytest.mark.asyncio
async def test_already_existing_task_is_success() -> None:
    client = Client(error=AlreadyExists("duplicate"))  # type: ignore[no-untyped-call]
    request = ActionTaskRequest(action_id=uuid7())

    task_name = await _enqueuer(client).enqueue(
        request,
        GmailActionCapability.CREATE_DRAFT,
    )

    assert task_name == client.requests[0]["task"]["name"]


@pytest.mark.asyncio
async def test_transient_enqueue_error_propagates() -> None:
    client = Client(error=ServiceUnavailable("retry"))  # type: ignore[no-untyped-call]

    with pytest.raises(ServiceUnavailable):
        await _enqueuer(client).enqueue(
            ActionTaskRequest(action_id=uuid7()),
            GmailActionCapability.CREATE_DRAFT,
        )
