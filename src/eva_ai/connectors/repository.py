from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid7

from sqlalchemy import Numeric, case, exists, literal, or_, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult

from eva_ai.connectors.gmail.contracts import WatchResult
from eva_ai.connectors.types import (
    ConnectorRecord,
    ConnectorStatus,
    GmailSyncRecord,
    SyncClaim,
)
from eva_ai.db.models import ConnectorAccount, GmailSyncState
from eva_ai.db.session import Database

_GMAIL_PROVIDER = "gmail"


class AmbiguousConnectorIdentity(RuntimeError):
    pass


class ConnectorScopeMismatchError(RuntimeError):
    pass


class ConnectorRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def reserve_gmail(
        self,
        user_id: UUID,
        workspace_id: UUID,
        account_identity: str,
        granted_scopes: tuple[str, ...],
        now: datetime,
    ) -> ConnectorRecord:
        identity = account_identity.lower()
        account_insert = (
            insert(ConnectorAccount)
            .values(
                id=uuid7(),
                user_id=user_id,
                workspace_id=workspace_id,
                provider=_GMAIL_PROVIDER,
                account_identity=identity,
                granted_scopes=list(granted_scopes),
                status=ConnectorStatus.CONNECTING,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_connector_accounts_workspace_provider_identity")
            .returning(ConnectorAccount)
        )
        async with self._database.session() as session:
            async with session.begin():
                account = (await session.scalars(account_insert)).one_or_none()
                if account is None:
                    account = await session.scalar(
                        select(ConnectorAccount).where(
                            ConnectorAccount.workspace_id == workspace_id,
                            ConnectorAccount.provider == _GMAIL_PROVIDER,
                            ConnectorAccount.account_identity == identity,
                        )
                    )
                if account is None:
                    raise LookupError("reserved connector was not found")
                if account.user_id != user_id:
                    raise ConnectorScopeMismatchError(
                        "connector user does not match persisted owner"
                    )
                if account.status == ConnectorStatus.ERROR:
                    account.status = ConnectorStatus.CONNECTING
                    account.last_error_type = None
                    account.last_error_summary = None
                sync_insert = (
                    insert(GmailSyncState)
                    .values(connector_account_id=account.id, created_at=now, updated_at=now)
                    .on_conflict_do_nothing()
                )
                await session.execute(sync_insert)
                return _connector_record(account)

    async def attach_secret(self, connector_id: UUID, secret_reference: str) -> ConnectorRecord:
        async with self._database.session() as session:
            async with session.begin():
                account = await session.get(ConnectorAccount, connector_id, with_for_update=True)
                if account is None:
                    raise LookupError("connector was not found")
                account.secret_reference = secret_reference
                return _connector_record(account)

    async def prepare_initial_watch(
        self,
        connector_id: UUID,
        history_id: str,
        connected_at: datetime,
    ) -> ConnectorRecord:
        async with self._database.session() as session:
            async with session.begin():
                account = await session.get(ConnectorAccount, connector_id, with_for_update=True)
                if account is None:
                    raise LookupError("connector was not found")
                if account.secret_reference is None:
                    raise ValueError("an initial watch boundary requires a secret reference")
                sync = await session.get(GmailSyncState, connector_id, with_for_update=True)
                if sync is None:
                    raise LookupError("Gmail sync state was not found")
                # The first verified profile is the durable lower cutover boundary. A retry
                # may refresh the watch, but cannot replace this cursor or timestamp.
                account.connected_at = account.connected_at or connected_at
                sync.history_id = sync.history_id or history_id
                return _connector_record(account)

    async def activate_initial_watch(
        self,
        connector_id: UUID,
        watch: WatchResult,
        next_renewal_at: datetime,
        next_safety_sync_at: datetime,
    ) -> ConnectorRecord:
        async with self._database.session() as session:
            async with session.begin():
                account = await session.get(ConnectorAccount, connector_id, with_for_update=True)
                if account is None:
                    raise LookupError("connector was not found")
                if account.status not in {
                    ConnectorStatus.CONNECTING,
                    ConnectorStatus.REAUTHORIZATION_REQUIRED,
                    ConnectorStatus.ACTIVE,
                }:
                    return _connector_record(account)
                if account.secret_reference is None:
                    raise ValueError("an active connector requires a secret reference")
                sync = await session.get(GmailSyncState, connector_id, with_for_update=True)
                if sync is None:
                    raise LookupError("Gmail sync state was not found")
                if account.connected_at is None or sync.history_id is None:
                    raise ValueError("an active connector requires an initial watch boundary")
                account.status = ConnectorStatus.ACTIVE
                account.last_error_type = None
                account.last_error_summary = None
                sync.watch_expiration = watch.expiration
                sync.next_watch_renewal_at = next_renewal_at
                sync.next_safety_sync_at = next_safety_sync_at
                return _connector_record(account)

    async def find_by_identity(self, account_identity: str) -> ConnectorRecord | None:
        statement = (
            select(ConnectorAccount)
            .where(
                ConnectorAccount.provider == _GMAIL_PROVIDER,
                ConnectorAccount.account_identity == account_identity.lower(),
            )
            .limit(2)
        )
        async with self._database.session() as session:
            accounts = (await session.scalars(statement)).all()
        if len(accounts) > 1:
            raise AmbiguousConnectorIdentity("Gmail identity maps to multiple connectors")
        return _connector_record(accounts[0]) if accounts else None

    async def get(self, connector_id: UUID) -> ConnectorRecord | None:
        async with self._database.session() as session:
            account = await session.get(ConnectorAccount, connector_id)
        return _connector_record(account) if account is not None else None

    async def get_sync_state(self, connector_id: UUID) -> GmailSyncRecord | None:
        async with self._database.session() as session:
            sync = await session.get(GmailSyncState, connector_id)
        return _sync_record(sync) if sync is not None else None

    async def record_notification(self, connector_id: UUID, observed_at: datetime) -> bool:
        monotonic_observation = case(
            (
                or_(
                    GmailSyncState.last_notification_at.is_(None),
                    GmailSyncState.last_notification_at < observed_at,
                ),
                observed_at,
            ),
            else_=GmailSyncState.last_notification_at,
        )
        statement = (
            update(GmailSyncState)
            .where(GmailSyncState.connector_account_id == connector_id)
            .values(last_notification_at=monotonic_observation)
        )
        return await self._updated(statement)

    async def claim_sync(
        self,
        connector_id: UUID,
        now: datetime,
        lease_seconds: int,
    ) -> SyncClaim | None:
        claim_id = uuid7()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        active_connector = exists(
            select(ConnectorAccount.id).where(
                ConnectorAccount.id == connector_id,
                ConnectorAccount.status == ConnectorStatus.ACTIVE,
            )
        )
        no_active_lease = or_(
            GmailSyncState.claim_id.is_(None),
            GmailSyncState.lease_expires_at.is_(None),
            GmailSyncState.lease_expires_at <= now,
        )
        statement = (
            update(GmailSyncState)
            .where(
                GmailSyncState.connector_account_id == connector_id,
                no_active_lease,
                active_connector,
            )
            .values(claim_id=claim_id, lease_expires_at=lease_expires_at)
            .returning(GmailSyncState)
        )
        async with self._database.session() as session:
            async with session.begin():
                sync = (await session.scalars(statement)).one_or_none()
                if sync is None:
                    return None
                account = await session.get(ConnectorAccount, connector_id)
                if account is None:
                    raise LookupError("claimed connector was not found")
                return SyncClaim(
                    claim_id=claim_id,
                    connector=_connector_record(account),
                    sync=_sync_record(sync),
                    lease_expires_at=lease_expires_at,
                )

    async def complete_sync(
        self,
        claim: SyncClaim,
        history_id: str,
        now: datetime,
        next_safety_sync_at: datetime,
    ) -> bool:
        if not history_id or not history_id.isdecimal():
            return False
        history_value = Decimal(history_id)
        cursor_is_not_lower = or_(
            GmailSyncState.history_id.is_(None),
            sql_cast(GmailSyncState.history_id, Numeric)
            <= sql_cast(literal(history_value), Numeric),
        )
        statement = (
            update(GmailSyncState)
            .where(
                GmailSyncState.connector_account_id == claim.connector.id,
                GmailSyncState.claim_id == claim.claim_id,
                cursor_is_not_lower,
            )
            .values(
                history_id=history_id,
                last_successful_sync_at=now,
                next_safety_sync_at=next_safety_sync_at,
                claim_id=None,
                lease_expires_at=None,
            )
        )
        return await self._updated(statement)

    async def complete_recovery(
        self,
        claim: SyncClaim,
        watch: WatchResult,
        now: datetime,
        next_renewal_at: datetime,
        next_safety_sync_at: datetime,
    ) -> bool:
        if not watch.history_id or not watch.history_id.isdecimal():
            return False
        history_value = Decimal(watch.history_id)
        cursor_is_not_lower = or_(
            GmailSyncState.history_id.is_(None),
            sql_cast(GmailSyncState.history_id, Numeric)
            <= sql_cast(literal(history_value), Numeric),
        )
        statement = (
            update(GmailSyncState)
            .where(
                GmailSyncState.connector_account_id == claim.connector.id,
                GmailSyncState.claim_id == claim.claim_id,
                cursor_is_not_lower,
            )
            .values(
                history_id=watch.history_id,
                watch_expiration=watch.expiration,
                last_successful_sync_at=now,
                next_watch_renewal_at=next_renewal_at,
                next_safety_sync_at=next_safety_sync_at,
                claim_id=None,
                lease_expires_at=None,
            )
        )
        return await self._updated(statement)

    async def release_sync(self, claim: SyncClaim) -> bool:
        statement = (
            update(GmailSyncState)
            .where(
                GmailSyncState.connector_account_id == claim.connector.id,
                GmailSyncState.claim_id == claim.claim_id,
            )
            .values(claim_id=None, lease_expires_at=None)
        )
        return await self._updated(statement)

    async def mark_reauthorization_required(self, connector_id: UUID, error: BaseException) -> None:
        connector_update = (
            update(ConnectorAccount)
            .where(ConnectorAccount.id == connector_id)
            .values(
                status=ConnectorStatus.REAUTHORIZATION_REQUIRED,
                last_error_type=type(error).__name__,
                last_error_summary="operation failed",
            )
        )
        clear_claim = (
            update(GmailSyncState)
            .where(GmailSyncState.connector_account_id == connector_id)
            .values(claim_id=None, lease_expires_at=None)
        )
        async with self._database.session() as session:
            async with session.begin():
                await session.execute(connector_update)
                await session.execute(clear_claim)

    async def mark_error(self, connector_id: UUID, error: BaseException) -> None:
        connector_update = (
            update(ConnectorAccount)
            .where(ConnectorAccount.id == connector_id)
            .values(
                status=ConnectorStatus.ERROR,
                last_error_type=type(error).__name__,
                last_error_summary="operation failed",
            )
        )
        clear_claim = (
            update(GmailSyncState)
            .where(GmailSyncState.connector_account_id == connector_id)
            .values(claim_id=None, lease_expires_at=None)
        )
        async with self._database.session() as session:
            async with session.begin():
                await session.execute(connector_update)
                await session.execute(clear_claim)

    async def due_for_maintenance(self, now: datetime) -> tuple[UUID, ...]:
        due = or_(
            GmailSyncState.next_watch_renewal_at <= now,
            GmailSyncState.next_safety_sync_at <= now,
        )
        statement = (
            select(ConnectorAccount.id)
            .join(GmailSyncState, GmailSyncState.connector_account_id == ConnectorAccount.id)
            .where(ConnectorAccount.status == ConnectorStatus.ACTIVE, due)
            .order_by(ConnectorAccount.id)
        )
        async with self._database.session() as session:
            return tuple((await session.scalars(statement)).all())

    async def record_watch_renewal(
        self,
        claim: SyncClaim,
        expiration: datetime,
        next_renewal_at: datetime,
    ) -> bool:
        # A renewal cannot replace the durable cursor captured by this lease.
        statement = (
            update(GmailSyncState)
            .where(
                GmailSyncState.connector_account_id == claim.connector.id,
                GmailSyncState.claim_id == claim.claim_id,
            )
            .values(
                watch_expiration=expiration,
                next_watch_renewal_at=next_renewal_at,
                claim_id=None,
                lease_expires_at=None,
            )
        )
        return await self._updated(statement)

    async def _updated(self, statement: Any) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                result = cast(CursorResult[Any], await session.execute(statement))
                return result.rowcount == 1


def _connector_record(account: ConnectorAccount) -> ConnectorRecord:
    return ConnectorRecord(
        id=account.id,
        user_id=account.user_id,
        workspace_id=account.workspace_id,
        provider=account.provider,
        account_identity=account.account_identity,
        granted_scopes=tuple(account.granted_scopes),
        status=ConnectorStatus(account.status),
        secret_reference=account.secret_reference,
        connected_at=account.connected_at,
    )


def _sync_record(sync: GmailSyncState) -> GmailSyncRecord:
    return GmailSyncRecord(
        connector_account_id=sync.connector_account_id,
        history_id=sync.history_id,
        watch_expiration=sync.watch_expiration,
        last_notification_at=sync.last_notification_at,
        last_successful_sync_at=sync.last_successful_sync_at,
        next_watch_renewal_at=sync.next_watch_renewal_at,
        next_safety_sync_at=sync.next_safety_sync_at,
        claim_id=sync.claim_id,
        lease_expires_at=sync.lease_expires_at,
    )
