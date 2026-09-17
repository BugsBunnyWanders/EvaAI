from uuid import uuid7

from eva_ai.actions.types import ActionExecutionRequestedMessage
from eva_ai.events.outbox import _parse_envelope


def test_relay_parses_action_execution_request_from_transactional_outbox() -> None:
    expected = ActionExecutionRequestedMessage(
        outbox_message_id=uuid7(),
        action_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
    )

    parsed = _parse_envelope(expected.message_type, expected.model_dump(mode="json"))

    assert parsed == expected
