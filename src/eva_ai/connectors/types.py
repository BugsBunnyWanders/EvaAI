from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


class ConnectorStatus(StrEnum):
    CONNECTING = "CONNECTING"
    ACTIVE = "ACTIVE"
    REAUTHORIZATION_REQUIRED = "REAUTHORIZATION_REQUIRED"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


class ConnectorRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: UUID
    workspace_id: UUID
    provider: str
    account_identity: str
    granted_scopes: tuple[str, ...]
    status: ConnectorStatus
    secret_reference: str | None
    connected_at: datetime | None

    @model_validator(mode="after")
    def require_aware_connected_at(self) -> ConnectorRecord:
        if self.connected_at is not None and self.connected_at.utcoffset() is None:
            raise ValueError("connected_at must include a timezone")
        return self


class GmailSyncRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    connector_account_id: UUID
    history_id: str | None
    watch_expiration: datetime | None
    last_notification_at: datetime | None
    last_successful_sync_at: datetime | None
    next_watch_renewal_at: datetime | None
    next_safety_sync_at: datetime | None
    claim_id: UUID | None
    lease_expires_at: datetime | None


class SyncClaim(BaseModel):
    model_config = ConfigDict(frozen=True)

    claim_id: UUID
    connector: ConnectorRecord
    sync: GmailSyncRecord
    lease_expires_at: datetime
