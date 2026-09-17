import hashlib
from typing import Protocol, cast

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2

from eva_ai.actions.types import ActionTaskRequest, GmailActionCapability


class CloudTasksClient(Protocol):
    def queue_path(self, project: str, location: str, queue: str) -> str: ...

    def task_path(
        self,
        project: str,
        location: str,
        queue: str,
        task: str,
    ) -> str: ...

    async def create_task(self, request: dict[str, object]) -> object: ...


class GoogleCloudTaskEnqueuer:
    def __init__(
        self,
        *,
        project_id: str,
        location: str,
        queue_id: str,
        executor_url: str,
        service_account_email: str,
        audience: str,
        client: CloudTasksClient | None = None,
    ) -> None:
        self._project_id = project_id
        self._location = location
        self._queue_id = queue_id
        self._executor_url = executor_url
        self._service_account_email = service_account_email
        self._audience = audience
        self._client = (
            client
            if client is not None
            else cast(CloudTasksClient, tasks_v2.CloudTasksAsyncClient())
        )

    async def enqueue(
        self,
        request: ActionTaskRequest,
        capability: GmailActionCapability,
    ) -> str:
        parent = self._client.queue_path(
            self._project_id,
            self._location,
            self._queue_id,
        )
        task_id = _task_id(str(request.action_id), capability)
        task_name = self._client.task_path(
            self._project_id,
            self._location,
            self._queue_id,
            task_id,
        )
        create_request: dict[str, object] = {
            "parent": parent,
            "task": {
                "name": task_name,
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": self._executor_url,
                    "headers": {"Content-Type": "application/json"},
                    # The private executor reloads every trusted field from PostgreSQL. Keeping the
                    # task body opaque prevents email content and tenant scope from entering Tasks.
                    "body": request.model_dump_json().encode("utf-8"),
                    "oidc_token": {
                        "service_account_email": self._service_account_email,
                        "audience": self._audience,
                    },
                },
            },
        }
        try:
            await self._client.create_task(request=create_request)
        except AlreadyExists:
            # A deterministic task collision is the successful result of Pub/Sub redelivery.
            pass
        return task_name


def _task_id(action_id: str, capability: GmailActionCapability) -> str:
    digest = hashlib.sha256(f"{action_id}:{capability}".encode()).hexdigest()
    return f"action-{digest}"
