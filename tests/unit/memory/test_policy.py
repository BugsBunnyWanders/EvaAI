from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from eva_ai.memory.errors import UnsafeMemoryError
from eva_ai.memory.policy import MemoryPolicy
from eva_ai.memory.types import MemoryFactDraft, MemoryScopeType, MemorySourceType

USER_ID = UUID("00000000-0000-7000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000002")


def draft(namespace: str, key: str) -> MemoryFactDraft:
    return MemoryFactDraft(
        user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        namespace=namespace,
        key=key,
        value_json={"value": "redacted"},
        scope_type=MemoryScopeType.WORKSPACE,
        scope_id=WORKSPACE_ID,
        source_type=MemorySourceType.USER_EXPLICIT,
        source_ref="operator:test",
        confidence=Decimal("1"),
        idempotency_key=f"test:{namespace}:{key}",
        valid_from=datetime(2026, 9, 6, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("namespace", "key"),
    [
        ("credentials", "gmail"),
        ("profile", "api_key"),
        ("profile", "oauth_refresh_token"),
        ("authorization", "can_delete_resources"),
        ("security", "private-key"),
    ],
)
def test_policy_rejects_secret_and_authority_slots(namespace: str, key: str) -> None:
    with pytest.raises(UnsafeMemoryError):
        MemoryPolicy().validate_fact(draft(namespace, key))


def test_policy_accepts_bounded_user_preference() -> None:
    MemoryPolicy().validate_fact(draft("career.preferences", "interview_time"))
