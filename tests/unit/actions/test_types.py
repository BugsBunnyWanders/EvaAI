from uuid import uuid4

from eva_ai.actions.types import (
    ActionExecutionRequestedMessage,
    ActionTaskRequest,
    ApprovalStatus,
    ManagedDraftStatus,
)


def test_execution_message_is_strict_and_scoped() -> None:
    action_id = uuid4()
    user_id = uuid4()
    workspace_id = uuid4()
    outbox_message_id = uuid4()

    message = ActionExecutionRequestedMessage(
        outbox_message_id=outbox_message_id,
        action_id=action_id,
        user_id=user_id,
        workspace_id=workspace_id,
    )

    assert message.message_type == "action.execution.requested"
    assert message.action_id == action_id
    assert message.user_id == user_id
    assert message.workspace_id == workspace_id
    assert message.schema_version == 1


def test_cloud_task_request_contains_only_opaque_action_identity() -> None:
    request = ActionTaskRequest(action_id=uuid4())

    assert set(request.model_dump()) == {"action_id", "schema_version"}


def test_terminal_state_names_are_stable() -> None:
    assert ApprovalStatus.SUPERSEDED == "SUPERSEDED"
    assert ManagedDraftStatus.UNKNOWN == "UNKNOWN"
