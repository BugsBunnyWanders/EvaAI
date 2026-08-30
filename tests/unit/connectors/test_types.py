from datetime import datetime
from uuid import uuid7

import pytest
from pydantic import ValidationError

from eva_ai.connectors.types import ConnectorRecord, ConnectorStatus


def test_connector_record_is_immutable() -> None:
    record = ConnectorRecord(
        id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        provider="gmail",
        account_identity="saswatray2505@gmail.com",
        granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        status=ConnectorStatus.CONNECTING,
        secret_reference=None,
        connected_at=None,
    )

    with pytest.raises(ValidationError):
        record.status = ConnectorStatus.ACTIVE  # type: ignore[misc]


def test_connector_record_rejects_naive_connected_at() -> None:
    with pytest.raises(ValidationError):
        ConnectorRecord(
            id=uuid7(),
            user_id=uuid7(),
            workspace_id=uuid7(),
            provider="gmail",
            account_identity="saswatray2505@gmail.com",
            granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
            status=ConnectorStatus.ACTIVE,
            secret_reference="projects/p/secrets/s",
            connected_at=datetime(2026, 8, 30),
        )
