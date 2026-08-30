# Milestone 2 Gmail Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect Eva to `saswatray2505@gmail.com` and persist each newly received inbox message exactly once as a canonical `email.received` Event through Gmail watch and Google Pub/Sub.

**Architecture:** Provider-neutral ConnectorAccount and GmailSyncState records hold ownership, health, and durable cursor state while Secret Manager holds OAuth credentials. Focused Google adapters implement OAuth, Gmail, Secret Manager, and Pub/Sub pull contracts; application services bootstrap the watch, serialize history synchronization with a database lease, normalize messages through the existing EventService, and run persisted renewal/recovery work. The local worker acknowledges Pub/Sub only after the Gmail range is durable.

**Tech Stack:** Python `>=3.14,<3.15`, Pydantic 2, SQLAlchemy 2 async ORM, PostgreSQL 17, Alembic, `google-api-python-client` 2.x, `google-auth-oauthlib` 1.x, `google-cloud-secret-manager` 2.x, `google-cloud-pubsub` 2.x, pytest/pytest-asyncio, Ruff, strict mypy, Docker Compose, gcloud CLI, GitHub CLI.

**Spec:** `docs/superpowers/specs/2026-08-30-milestone-2-gmail-ingestion-design.md`

## Global Constraints

- Work on `codex/milestone-2-gmail-ingestion`; push that branch and open a pull request to `main` only after automated and live verification pass.
- Use the package import path `eva_ai` and distribution name `eva-ai`.
- Support Python `>=3.14,<3.15` and PostgreSQL 17.
- Use GCP project `evaai-507018`, topic `eva-gmail-notifications`, and pull subscription `eva-gmail-ingestion-local`.
- Connect only `saswatray2505@gmail.com` in this milestone.
- Request exactly `https://www.googleapis.com/auth/gmail.readonly`; do not add send, compose, modify, labels, or full-mail scopes.
- Ingest only messages whose Gmail `internalDate` is at or after the connector's first `connected_at` and whose current labels include `INBOX`.
- Include Primary, Promotions, Social, and Updates; preserve all Gmail label IDs.
- Store headers, plain text, decoded HTML, and attachment metadata; never download attachment binaries.
- Treat all email content as untrusted data. It cannot supply User/Workspace identity, authorization, policy, approval, or application instructions.
- Store authorized-user credentials only in Secret Manager. PostgreSQL contains a secret resource reference, never OAuth codes, client secrets, refresh tokens, or access tokens.
- Preserve at-least-once semantics. Advance the history cursor only after all Events in the range are durable; make replay harmless through `gmail:{connector_account_id}:{message_id}:received`.
- A watch renewal updates expiration/scheduling fields but never overwrites the durable history cursor.
- Use database claims only for short state transactions; Gmail, Pub/Sub, and Secret Manager network calls occur without a database transaction held open.
- Logs never include tokens, OAuth payloads, subjects, bodies, full address lists, or attachment content.
- Add comments where they explain cursor, acknowledgement, lease, OAuth, MIME, replay, or security invariants; do not narrate self-evident code.
- Use test-driven development: run each focused test and observe the intended failure before writing production code.
- The automated suite must use fakes at every Google network boundary and must never read the personal mailbox or real credentials.
- Cloud Run, public webhooks, attachment ingestion, Gmail tools, relevance, Goals, Situations, Memory, Agents, drafts, and sends remain outside this plan.

---

## File Map

### New production files

- `src/eva_ai/connectors/__init__.py` — public connector exports.
- `src/eva_ai/connectors/types.py` — connector status, immutable connector/sync records, and synchronization claim types.
- `src/eva_ai/connectors/repository.py` — ConnectorAccount lifecycle, lookup, due-work queries, and claim-protected cursor updates.
- `src/eva_ai/connectors/gmail/__init__.py` — Gmail connector exports.
- `src/eva_ai/connectors/gmail/contracts.py` — OAuth, Gmail, credential-store, and pull-subscriber protocols plus provider DTOs/errors.
- `src/eva_ai/connectors/gmail/notification.py` — Pub/Sub data validation and GmailNotification decoding.
- `src/eva_ai/connectors/gmail/normalizer.py` — nested MIME traversal and canonical NewEvent construction.
- `src/eva_ai/connectors/gmail/bootstrap.py` — OAuth account verification, secret versioning, ConnectorAccount preparation, and initial watch.
- `src/eva_ai/connectors/gmail/sync.py` — lease-based Gmail history pagination, message fetch, Event ingestion, and cursor advancement.
- `src/eva_ai/connectors/gmail/maintenance.py` — daily watch renewal, notification-silence safety sync, and expired-history bounded recovery.
- `src/eva_ai/connectors/gmail/worker.py` — one pull batch, acknowledgement decisions, and continuous local orchestration.
- `src/eva_ai/integrations/gcp/secret_manager.py` — Secret Manager credential-store adapter.
- `src/eva_ai/integrations/gcp/subscriber.py` — Pub/Sub synchronous-pull adapter exposed through an async contract.
- `src/eva_ai/integrations/gmail/__init__.py` — Gmail integration package marker.
- `src/eva_ai/integrations/gmail/api.py` — official Gmail API adapter and error classification.
- `src/eva_ai/integrations/gmail/oauth.py` — desktop OAuth loopback adapter.
- `src/eva_ai/cli.py` — `scope create`, `gmail connect`, `gmail pull`, and `gmail maintain` commands.
- `src/eva_ai/local_scope.py` — explicit local User/Workspace bootstrap used before OAuth.
- `migrations/versions/20260830_0003_gmail_connector.py` — ConnectorAccount and GmailSyncState schema.
- `docs/gmail-setup.md` — OAuth Console, gcloud, local connection, smoke-test, recovery, and teardown guide.

### New test files

- `tests/unit/connectors/test_types.py`
- `tests/integration/connectors/test_repository.py`
- `tests/unit/connectors/gmail/test_notification.py`
- `tests/unit/connectors/gmail/test_normalizer.py`
- `tests/unit/connectors/gmail/test_bootstrap.py`
- `tests/unit/connectors/gmail/test_sync.py`
- `tests/unit/connectors/gmail/test_maintenance.py`
- `tests/unit/connectors/gmail/test_worker.py`
- `tests/unit/integrations/gcp/test_secret_manager.py`
- `tests/unit/integrations/gcp/test_subscriber.py`
- `tests/unit/integrations/gmail/test_api.py`
- `tests/unit/integrations/gmail/test_oauth.py`
- `tests/integration/connectors/test_gmail_ingestion.py`
- `tests/unit/test_cli.py`

### Modified files

- `pyproject.toml` and `uv.lock` — Google Gmail/OAuth/Secret Manager dependencies and `eva` console script.
- `src/eva_ai/config.py` — Gmail topic, subscription, account, OAuth file, lease, pull timeout, renewal, and safety-sync settings.
- `src/eva_ai/db/models/__init__.py` — register connector ORM models.
- `src/eva_ai/logging.py` — permit only non-content Gmail operational identifiers.
- `tests/integration/factories.py` — ConnectorAccount/GmailSyncState fixtures.
- `tests/integration/events/test_schema.py` — connector server-default assertions.
- `tests/integration/test_migrations.py` — Milestone 2 table/constraint assertions.
- `.gitignore` — ignore `.secrets/` and downloaded OAuth client files.
- `.env.example` — non-secret Gmail settings.
- `README.md` and `Makefile` — Gmail setup, worker, and maintenance commands.

---

### Task 1: Dependencies, Settings, and Provider Contracts

**Files:**
- Create: `src/eva_ai/connectors/__init__.py`
- Create: `src/eva_ai/connectors/types.py`
- Create: `src/eva_ai/connectors/gmail/__init__.py`
- Create: `src/eva_ai/connectors/gmail/contracts.py`
- Modify: `src/eva_ai/config.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/unit/connectors/test_types.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: existing `Settings`, `Database`, `NewEvent`, UUID7 IDs, UTC-aware datetimes.
- Produces: `ConnectorStatus`, `ConnectorRecord`, `GmailSyncRecord`, `SyncClaim`, `GmailNotification`, `WatchResult`, `HistoryPage`, `MessageListPage`, `AuthorizedUserGrant`, `GmailClient`, `GmailClientFactory`, `OAuthAuthorizer`, `CredentialStore`, `PullMessage`, `PullSubscriber`, `HistoryCursorExpired`, `AuthorizationRevoked`, and Gmail Settings fields.

- [ ] **Step 1: Write failing settings and immutable-type tests**

```python
# tests/unit/connectors/test_types.py
from datetime import UTC, datetime
from uuid import uuid7

import pytest
from pydantic import ValidationError

from eva_ai.connectors.types import ConnectorRecord, ConnectorStatus


def test_connector_record_is_immutable() -> None:
    record = ConnectorRecord(
        id=uuid7(), user_id=uuid7(), workspace_id=uuid7(), provider="gmail",
        account_identity="saswatray2505@gmail.com",
        granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        status=ConnectorStatus.CONNECTING, secret_reference=None, connected_at=None,
    )
    with pytest.raises(ValidationError):
        record.status = ConnectorStatus.ACTIVE  # type: ignore[misc]


def test_connector_record_rejects_naive_connected_at() -> None:
    with pytest.raises(ValidationError):
        ConnectorRecord(
            id=uuid7(), user_id=uuid7(), workspace_id=uuid7(), provider="gmail",
            account_identity="saswatray2505@gmail.com",
            granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
            status=ConnectorStatus.ACTIVE, secret_reference="projects/p/secrets/s",
            connected_at=datetime(2026, 8, 30),
        )
```

```python
# append to tests/unit/test_config.py
def test_gmail_settings_have_safe_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.gmail_topic_id == "eva-gmail-notifications"
    assert settings.gmail_subscription_id == "eva-gmail-ingestion-local"
    assert settings.gmail_account is None
    assert settings.gmail_oauth_client_file is None
    assert settings.gmail_sync_lease_seconds == 300
    assert settings.gmail_pull_timeout_seconds == 30
    assert settings.gmail_watch_renewal_hours == 24
    assert settings.gmail_safety_sync_minutes == 60
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/unit/connectors/test_types.py tests/unit/test_config.py -v`

Expected: collection fails because `eva_ai.connectors` and the Gmail settings do not exist.

- [ ] **Step 3: Add Google dependencies and console entry point**

Run:

```bash
uv add "google-api-python-client>=2,<3" "google-auth-oauthlib>=1,<2" "google-cloud-secret-manager>=2,<3"
```

Add to `pyproject.toml`:

```toml
[project.scripts]
eva = "eva_ai.cli:main"
```

Expected: `pyproject.toml` and `uv.lock` contain the three bounded dependencies.

- [ ] **Step 4: Implement immutable connector types and exact provider protocols**

```python
# src/eva_ai/connectors/types.py
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
    def require_aware_connected_at(self) -> "ConnectorRecord":
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
```

Define frozen dataclasses, typed errors, and `Protocol` classes in `contracts.py` with these exact signatures:

```python
@dataclass(frozen=True, slots=True)
class GmailNotification:
    email_address: str
    history_id: str


@dataclass(frozen=True, slots=True)
class WatchResult:
    history_id: str
    expiration: datetime


@dataclass(frozen=True, slots=True)
class HistoryPage:
    message_ids: tuple[str, ...]
    history_id: str
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class MessageListPage:
    message_ids: tuple[str, ...]
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class AuthorizedUserGrant:
    authorized_user_json: str


@dataclass(frozen=True, slots=True)
class PullMessage:
    ack_id: str
    message_id: str
    data: bytes


class InvalidNotification(ValueError):
    pass


class HistoryCursorExpired(RuntimeError):
    pass


class AuthorizationRevoked(RuntimeError):
    pass


class GmailClient(Protocol):
    async def get_profile(self) -> str: ...
    async def watch(self, topic_name: str) -> WatchResult: ...
    async def list_history(self, start_history_id: str, page_token: str | None) -> HistoryPage: ...
    async def get_message(self, message_id: str) -> Mapping[str, object]: ...
    async def list_message_ids(self, query: str, page_token: str | None) -> MessageListPage: ...

class GmailClientFactory(Protocol):
    async def create(self, authorized_user_json: str) -> GmailClient: ...

class OAuthAuthorizer(Protocol):
    async def authorize(self, client_file: Path, scopes: tuple[str, ...]) -> AuthorizedUserGrant: ...

class CredentialStore(Protocol):
    async def put(self, connector_id: UUID, authorized_user_json: str) -> str: ...
    async def get(self, secret_reference: str) -> str: ...

class PullSubscriber(Protocol):
    async def pull(self, max_messages: int, timeout_seconds: int) -> tuple[PullMessage, ...]: ...
    async def acknowledge(self, ack_ids: tuple[str, ...]) -> None: ...
    async def negative_acknowledge(self, ack_ids: tuple[str, ...]) -> None: ...
    async def close(self) -> None: ...
```

All provider identifiers remain strings. `authorized_user_json` and message data must use `repr=False` wherever their container type could otherwise render them.

- [ ] **Step 5: Add validated Gmail settings**

Add `gmail_topic_id`, `gmail_subscription_id`, `gmail_account`, `gmail_oauth_client_file`, `gmail_sync_lease_seconds`, `gmail_pull_timeout_seconds`, `gmail_watch_renewal_hours`, and `gmail_safety_sync_minutes` to `Settings`. Reuse `pubsub_project_id` as the one GCP project setting and reject blank topic, subscription, and configured account strings.

- [ ] **Step 6: Run tests, static checks, and commit**

Run:

```bash
uv run pytest tests/unit/connectors/test_types.py tests/unit/test_config.py -v
uv run ruff check src/eva_ai/connectors src/eva_ai/config.py tests/unit/connectors tests/unit/test_config.py
uv run mypy src/eva_ai/connectors src/eva_ai/config.py
```

Expected: all commands pass.

Commit:

```bash
git add pyproject.toml uv.lock src/eva_ai/connectors src/eva_ai/config.py tests/unit/connectors tests/unit/test_config.py
git commit -m "feat: define Gmail connector contracts"
```

---

### Task 2: Connector Persistence and Migration

**Files:**
- Create: `src/eva_ai/db/models/connectors.py`
- Create: `migrations/versions/20260830_0003_gmail_connector.py`
- Modify: `src/eva_ai/db/models/__init__.py`
- Modify: `tests/integration/factories.py`
- Modify: `tests/integration/events/test_schema.py`
- Modify: `tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: `ConnectorStatus`, existing User/Workspace composite ownership, timestamp/UUID mixins.
- Produces: ORM `ConnectorAccount` and `GmailSyncState` registered in `Base.metadata`.

- [ ] **Step 1: Write failing migration and schema tests**

Add assertions that `connector_accounts` and `gmail_sync_states` exist after `alembic upgrade head`, and test these database guarantees:

```python
assert constraint_names("connector_accounts") >= {
    "fk_connector_accounts_workspace_user",
    "uq_connector_accounts_workspace_provider_identity",
    "ck_connector_accounts_status",
    "ck_connector_accounts_active_secret",
}
assert primary_key_columns("gmail_sync_states") == ["connector_account_id"]
```

Add ORM-default cases for `created_at`, `updated_at`, and `status="CONNECTING"` using the existing parametrized schema-test pattern.

- [ ] **Step 2: Run migration tests and verify RED**

Run: `uv run pytest tests/integration/test_migrations.py tests/integration/events/test_schema.py -v`

Expected: FAIL because the two tables and ORM classes do not exist.

- [ ] **Step 3: Implement the ORM schema**

Create `ConnectorAccount` with UUID `id`, `user_id`, `workspace_id`, `provider` (100), `account_identity` (320), `granted_scopes` as `ARRAY(Text)`, status (40), nullable `secret_reference` (500), nullable `connected_at`, nullable `last_error_type` (200), and nullable `last_error_summary` (500). Add the approved composite ownership foreign key, unique identity key, status check, and `status != 'ACTIVE' OR secret_reference IS NOT NULL` check.

Create `GmailSyncState` with ConnectorAccount PK/FK, nullable `history_id` (100), watch expiration, last notification, last successful sync, next watch renewal, next safety sync, nullable UUID `claim_id`, nullable lease expiration, and timestamps. Add an index over due renewal/safety fields.

- [ ] **Step 4: Write the explicit Alembic migration**

Implement upgrade in dependency order and downgrade in reverse order. Use PostgreSQL UUID, ARRAY(Text), timezone-aware timestamps, named constraints, and server defaults matching the ORM. Do not autogenerate an unreviewed migration.

- [ ] **Step 5: Run migration tests from a clean database and commit**

Run:

```bash
docker compose up -d --wait postgres
uv run alembic downgrade 20260830_0002
uv run alembic upgrade head
uv run pytest tests/integration/test_migrations.py tests/integration/events/test_schema.py -v
```

Expected: all tests pass and both upgrade and downgrade/upgrade paths succeed.

Commit:

```bash
git add src/eva_ai/db/models migrations/versions/20260830_0003_gmail_connector.py tests/integration
git commit -m "feat: persist Gmail connector state"
```

---

### Task 3: Claim-Protected Connector Repository

**Files:**
- Create: `src/eva_ai/connectors/repository.py`
- Test: `tests/integration/connectors/test_repository.py`
- Modify: `tests/integration/factories.py`

**Interfaces:**
- Consumes: `Database`, `ConnectorAccount`, `GmailSyncState`, `ConnectorRecord`, `GmailSyncRecord`, `SyncClaim`.
- Produces: `ConnectorRepository.reserve_gmail()`, `attach_secret()`, `activate_initial_watch()`, `find_by_identity()`, `get()`, `claim_sync()`, `complete_sync()`, `release_sync()`, `mark_reauthorization_required()`, `due_for_maintenance()`, and renewal timestamp updates.

Use these exact repository signatures:

```python
async def reserve_gmail(self, user_id: UUID, workspace_id: UUID, account_identity: str,
                        granted_scopes: tuple[str, ...], now: datetime) -> ConnectorRecord: ...
async def attach_secret(self, connector_id: UUID, secret_reference: str) -> ConnectorRecord: ...
async def activate_initial_watch(self, connector_id: UUID, watch: WatchResult,
                                 now: datetime, next_renewal_at: datetime,
                                 next_safety_sync_at: datetime) -> ConnectorRecord: ...
async def find_by_identity(self, account_identity: str) -> ConnectorRecord | None: ...
async def get(self, connector_id: UUID) -> ConnectorRecord | None: ...
async def get_sync_state(self, connector_id: UUID) -> GmailSyncRecord | None: ...
async def claim_sync(self, connector_id: UUID, now: datetime,
                     lease_seconds: int) -> SyncClaim | None: ...
async def complete_sync(self, claim: SyncClaim, history_id: str, now: datetime,
                        next_safety_sync_at: datetime) -> bool: ...
async def release_sync(self, claim: SyncClaim) -> bool: ...
async def mark_reauthorization_required(self, connector_id: UUID,
                                        error_type: str) -> None: ...
async def due_for_maintenance(self, now: datetime) -> tuple[UUID, ...]: ...
async def record_watch_renewal(self, claim: SyncClaim, expiration: datetime,
                               next_renewal_at: datetime) -> bool: ...
```

- [ ] **Step 1: Write failing lifecycle, ownership, and claim tests**

Cover reservation in `CONNECTING`, uniqueness within a Workspace, lowercase Gmail identity, active-secret enforcement, initial activation preserving the first `connected_at`, reauthorization without resetting `connected_at`, two concurrent claimers receiving one claim, expired-lease reclamation, stale-claim completion rejection, and cursor monotonicity.

Use this core assertion for renewal safety:

```python
claim = await repository.claim_sync(connector.id, now, lease_seconds=300)
assert claim is not None
await repository.record_watch_renewal(
    claim, expiration=new_expiration, next_renewal_at=next_renewal
)
state = await repository.get_sync_state(connector.id)
assert state.history_id == "durable-before-renewal"
```

- [ ] **Step 2: Run repository tests and verify RED**

Run: `uv run pytest tests/integration/connectors/test_repository.py -v`

Expected: collection fails because `ConnectorRepository` does not exist.

- [ ] **Step 3: Implement short-transaction lifecycle methods**

Use PostgreSQL `INSERT ... ON CONFLICT` only where the conflict behavior is explicit. `reserve_gmail()` creates or loads `(workspace_id, "gmail", normalized_identity)` and returns the stable connector UUID. `activate_initial_watch()` uses `connected_at = coalesce(connected_at, now)` and writes the initial history cursor only while state is `CONNECTING` or reauthorizing.

- [ ] **Step 4: Implement synchronization claim methods**

`claim_sync(connector_id, now, lease_seconds)` atomically assigns a fresh UUID7 when no active claim exists or the lease expired, then returns an immutable `SyncClaim` containing connector/sync snapshots. `complete_sync()` and `release_sync()` update only `WHERE claim_id = :claim_id`; return `False` for stale claims. Cursor completion rejects a lower numeric history ID and never accepts a blank ID.

- [ ] **Step 5: Implement due-work and health transitions**

`due_for_maintenance(now)` returns active connector IDs with renewal or safety timestamps due. `record_watch_renewal()` updates only the matching claim, preserves history ID, writes watch scheduling fields, and clears the claim. `mark_reauthorization_required()` clears claim state, stores only a sanitized error class plus `operation failed`, and leaves history/connected_at untouched. Notification timestamps never alter the cursor.

- [ ] **Step 6: Run repository tests and commit**

Run:

```bash
uv run pytest tests/integration/connectors/test_repository.py -v
uv run ruff check src/eva_ai/connectors/repository.py tests/integration/connectors
uv run mypy src/eva_ai/connectors/repository.py tests/integration/connectors
```

Expected: all commands pass.

Commit:

```bash
git add src/eva_ai/connectors/repository.py tests/integration/connectors tests/integration/factories.py
git commit -m "feat: manage Gmail connector cursors"
```

---

### Task 4: Notification Decoder and Canonical Email Normalizer

**Files:**
- Create: `src/eva_ai/connectors/gmail/notification.py`
- Create: `src/eva_ai/connectors/gmail/normalizer.py`
- Test: `tests/unit/connectors/gmail/test_notification.py`
- Test: `tests/unit/connectors/gmail/test_normalizer.py`

**Interfaces:**
- Consumes: Pub/Sub client-decoded `bytes`, raw Gmail message mappings, `ConnectorRecord`, `NewEvent`, `PrincipalType.EXTERNAL`.
- Produces: `decode_notification(data: bytes) -> GmailNotification` and `normalize_message(raw, connector, history_id) -> NewEvent`.

- [ ] **Step 1: Write failing notification-decoder tests**

```python
def test_decode_notification_uses_client_decoded_bytes() -> None:
    notification = decode_notification(
        b'{"emailAddress":"SaswatRay2505@gmail.com","historyId":"12345"}'
    )
    assert notification.email_address == "saswatray2505@gmail.com"
    assert notification.history_id == "12345"


@pytest.mark.parametrize("data", [b"", b"not-json", b"[]", b'{"historyId":"1"}'])
def test_decode_notification_rejects_malformed_data(data: bytes) -> None:
    with pytest.raises(InvalidNotification):
        decode_notification(data)
```

- [ ] **Step 2: Write failing MIME and Event mapping tests**

Create fixtures for single-part text/plain, multipart/alternative, nested multipart/mixed, base64url without padding, ISO-8859-1 charset, inline text, a binary attachment with `attachmentId`, missing optional headers, Promotions labels, and pre-connection timestamps. Assert:

```python
event = normalize_message(raw_message, connector, history_id="900")
assert event.source == "gmail"
assert event.event_type == "email.received"
assert event.idempotency_key == f"gmail:{connector.id}:msg-1:received"
assert event.principal_type is PrincipalType.EXTERNAL
assert event.principal_id == connector.id
assert event.correlation_keys == ["gmail-thread:thread-1"]
assert event.payload["plain_text"] == "Hello Eva"
assert event.payload["html"] == "<p>Hello Eva</p>"
assert event.payload["attachments"] == [{
    "filename": "brief.pdf", "mime_type": "application/pdf",
    "size": 42, "attachment_id": "attachment-1",
}]
```

- [ ] **Step 3: Run parser tests and verify RED**

Run: `uv run pytest tests/unit/connectors/gmail/test_notification.py tests/unit/connectors/gmail/test_normalizer.py -v`

Expected: collection fails because decoder and normalizer modules do not exist.

- [ ] **Step 4: Implement strict notification decoding**

Decode UTF-8 JSON directly from `Message.data`; do not base64-decode it again. Require an object with nonblank string `emailAddress` and decimal-string `historyId`, lowercase the address, and raise `InvalidNotification` with a fixed content-free message.

- [ ] **Step 5: Implement recursive MIME normalization**

Use `email.utils.parseaddr`, `base64.urlsafe_b64decode` with calculated padding, and declared charset with UTF-8 replacement fallback. Traverse every nested `parts` list. Collect readable inline `text/plain` and `text/html` separately; collect any named or attachment-ID part as metadata without calling the attachment API. Retain decoded HTML as untrusted data and never render it in this task.

- [ ] **Step 6: Build the exact canonical NewEvent**

Convert `internalDate` milliseconds to an aware UTC datetime; require message ID, thread ID, and internalDate. Preserve selected headers as strings, all label IDs, snippet, plain text, HTML, and attachment metadata. Normalize sender name/address into actor data and set schema version 1. Add content-free normalization warning codes for recoverable charset/header defects.

- [ ] **Step 7: Run tests and commit**

Run:

```bash
uv run pytest tests/unit/connectors/gmail/test_notification.py tests/unit/connectors/gmail/test_normalizer.py -v
uv run ruff check src/eva_ai/connectors/gmail tests/unit/connectors/gmail
uv run mypy src/eva_ai/connectors/gmail/notification.py src/eva_ai/connectors/gmail/normalizer.py
```

Expected: all commands pass.

Commit:

```bash
git add src/eva_ai/connectors/gmail tests/unit/connectors/gmail
git commit -m "feat: normalize Gmail received events"
```

---

### Task 5: Google OAuth, Gmail API, Secret Manager, and Pull Adapters

**Files:**
- Create: `src/eva_ai/integrations/gmail/__init__.py`
- Create: `src/eva_ai/integrations/gmail/oauth.py`
- Create: `src/eva_ai/integrations/gmail/api.py`
- Create: `src/eva_ai/integrations/gcp/secret_manager.py`
- Create: `src/eva_ai/integrations/gcp/subscriber.py`
- Test: `tests/unit/integrations/gmail/test_oauth.py`
- Test: `tests/unit/integrations/gmail/test_api.py`
- Test: `tests/unit/integrations/gcp/test_secret_manager.py`
- Test: `tests/unit/integrations/gcp/test_subscriber.py`

**Interfaces:**
- Consumes: Task 1 protocols and Google client libraries.
- Produces: `GoogleDesktopOAuthAuthorizer`, `GoogleGmailClientFactory`, `GoogleGmailClient`, `GoogleSecretManagerCredentialStore`, and `GooglePullSubscriber`.

- [ ] **Step 1: Write failing adapter contract tests with injected Google fakes**

Assert OAuth calls `InstalledAppFlow.from_client_secrets_file()` with exactly `(GMAIL_READONLY_SCOPE,)`, runs a localhost server with offline access and consent prompting, and serializes authorized-user credentials without logging them. Assert Gmail watch uses `labelIds=["INBOX"]`, `labelFilterBehavior="INCLUDE"`, and the exact fully qualified topic. Assert history uses `historyTypes=["messageAdded"]`, message fetch uses `format="full"`, and list recovery uses the supplied query/page token.

These adapter tests cover the official Gmail `users.watch`, `users.history.list`, `users.messages.get`, and `users.messages.list` request shapes without making network calls.

Assert Secret Manager creates `eva-gmail-oauth-{connector_id}` only when absent, always adds a new UTF-8 secret version, returns a reference shaped like `projects/evaai-507018/secrets/eva-gmail-oauth-0191cafe-7b00-7000-8000-000000000001`, and loads `versions/latest`. Assert Pub/Sub builds the exact subscription path, returns decoded `message.data` bytes, and maps ack/nack IDs correctly.

- [ ] **Step 2: Run adapter tests and verify RED**

Run: `uv run pytest tests/unit/integrations/gmail tests/unit/integrations/gcp/test_secret_manager.py tests/unit/integrations/gcp/test_subscriber.py -v`

Expected: collection fails because the adapters do not exist.

- [ ] **Step 3: Implement OAuth and Gmail adapters**

Wrap blocking Google calls, including client construction, with `asyncio.to_thread`. Build Gmail services from `google.oauth2.credentials.Credentials.from_authorized_user_info()` and `googleapiclient.discovery.build("gmail", "v1", cache_discovery=False)`. Convert provider responses into Task 1 DTOs. Map HTTP 404 from `history.list` to `HistoryCursorExpired`, token refresh `invalid_grant` to `AuthorizationRevoked`, and leave rate limits/server/network errors retryable without embedding response bodies in exception text.

- [ ] **Step 4: Implement Secret Manager and Pub/Sub pull adapters**

Use Application Default Credentials through official clients. `GooglePullSubscriber.pull()` catches only `google.api_core.exceptions.DeadlineExceeded` and returns an empty tuple; other failures propagate for bounded retry. Negative acknowledgement uses `modify_ack_deadline(..., ack_deadline_seconds=0)`.

- [ ] **Step 5: Run adapter tests and commit**

Run:

```bash
uv run pytest tests/unit/integrations/gmail tests/unit/integrations/gcp -v
uv run ruff check src/eva_ai/integrations tests/unit/integrations
uv run mypy src/eva_ai/integrations tests/unit/integrations
```

Expected: all commands pass.

Commit:

```bash
git add src/eva_ai/integrations tests/unit/integrations
git commit -m "feat: add Google Gmail ingestion adapters"
```

---

### Task 6: OAuth Bootstrap and Initial Gmail Watch

**Files:**
- Create: `src/eva_ai/connectors/gmail/bootstrap.py`
- Test: `tests/unit/connectors/gmail/test_bootstrap.py`

**Interfaces:**
- Consumes: `ConnectorRepository`, `OAuthAuthorizer`, `CredentialStore`, `GmailClientFactory`, explicit User/Workspace IDs, expected Gmail address, OAuth client path, project/topic.
- Produces: `GmailBootstrapService.connect(command: ConnectGmail) -> ConnectorRecord`.

`ConnectGmail` is a frozen dataclass with `user_id: UUID`, `workspace_id: UUID`, `expected_identity: str`, `client_file: Path`, and `topic_name: str`. Define `AccountIdentityMismatch` in `bootstrap.py` as a content-free `ValueError`. Inject `clock: Callable[[], datetime]` into the service constructor.

- [ ] **Step 1: Write failing bootstrap orchestration tests**

Test exact call order and failure state for: correct account; OAuth profile mismatch; secret-store failure; watch failure; reconnect preserving original `connected_at`; and an immediate notification observing `CONNECTING`. Use a fixed clock and assert the initial watch response cursor is stored only after watch succeeds.

- [ ] **Step 2: Run bootstrap tests and verify RED**

Run: `uv run pytest tests/unit/connectors/gmail/test_bootstrap.py -v`

Expected: collection fails because `GmailBootstrapService` does not exist.

- [ ] **Step 3: Implement the bootstrap transaction sequence**

Implement:

```python
grant = await authorizer.authorize(command.client_file, (GMAIL_READONLY_SCOPE,))
gmail = await client_factory.create(grant.authorized_user_json)
actual_identity = (await gmail.get_profile()).lower()
if actual_identity != command.expected_identity.lower():
    raise AccountIdentityMismatch("authorized Gmail account does not match configuration")
connector = await repository.reserve_gmail(command.scope, actual_identity, scopes, now)
secret_reference = await credential_store.put(connector.id, grant.authorized_user_json)
await repository.attach_secret(connector.id, secret_reference)
watch = await gmail.watch(command.topic_name)
await repository.activate_initial_watch(connector.id, watch, now, renewal_due_at(watch, now))
```

Do not log the grant or credential JSON. On failure, leave a non-active connector with a sanitized status reason; re-running the command resumes with the same connector ID.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
uv run pytest tests/unit/connectors/gmail/test_bootstrap.py -v
uv run ruff check src/eva_ai/connectors/gmail/bootstrap.py tests/unit/connectors/gmail/test_bootstrap.py
uv run mypy src/eva_ai/connectors/gmail/bootstrap.py
```

Expected: all commands pass.

Commit:

```bash
git add src/eva_ai/connectors/gmail/bootstrap.py tests/unit/connectors/gmail/test_bootstrap.py
git commit -m "feat: bootstrap Gmail OAuth watch"
```

---

### Task 7: Lease-Based Gmail History Synchronization

**Files:**
- Create: `src/eva_ai/connectors/gmail/sync.py`
- Test: `tests/unit/connectors/gmail/test_sync.py`
- Test: `tests/integration/connectors/test_gmail_ingestion.py`

**Interfaces:**
- Consumes: `ConnectorRepository`, `CredentialStore`, `GmailClientFactory`, `EventService`, `decode_notification`, `normalize_message`.
- Produces: `GmailSyncService.handle(notification: GmailNotification) -> SyncResult` and `sync_connector(connector_id: UUID) -> SyncResult`, where result status is `SYNCED`, `ALREADY_COVERED`, `BUSY`, `REAUTHORIZATION_REQUIRED`, or `UNKNOWN_ACCOUNT`.

Define the exact result contract in `sync.py`:

```python
class SyncStatus(StrEnum):
    SYNCED = "SYNCED"
    ALREADY_COVERED = "ALREADY_COVERED"
    BUSY = "BUSY"
    REAUTHORIZATION_REQUIRED = "REAUTHORIZATION_REQUIRED"
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"


@dataclass(frozen=True, slots=True)
class SyncResult:
    status: SyncStatus
    connector_id: UUID | None
    events_created: int
    final_history_id: str | None
```

- [ ] **Step 1: Write failing pagination, filtering, and cursor tests**

Cover multiple history pages, the same message on multiple pages, current-label removal, a message older than `connected_at`, empty history, out-of-order notification hints, active claim busy, credential revocation, transient errors, and a crash-style retry where Event persistence succeeds but cursor completion fails.

Assert network calls occur while no SQLAlchemy transaction is open by using the existing session-state test pattern from EventProcessor tests.

- [ ] **Step 2: Write the failing PostgreSQL replay integration test**

Ingest a Gmail message with a fake client, force `complete_sync()` to fail once, rerun the same notification, then assert one Event, one EventProcessing, one OutboxMessage, and the final history cursor.

- [ ] **Step 3: Run synchronization tests and verify RED**

Run: `uv run pytest tests/unit/connectors/gmail/test_sync.py tests/integration/connectors/test_gmail_ingestion.py -v`

Expected: collection fails because `GmailSyncService` does not exist.

- [ ] **Step 4: Implement forward synchronization**

Resolve identity only through `repository.find_by_identity()`. Treat notification history ID as a wake-up hint, then delegate to `sync_connector(connector_id)`. The one-shot method claims the persisted connector, loads credentials, asynchronously constructs the Gmail client, and pages from `claim.sync.history_id`. Deduplicate message IDs in insertion order, fetch full messages, require `INBOX`, and require `occurred_at >= connected_at` before calling `EventService.ingest()`.

After all pages and Events are durable, call `complete_sync(claim, final_history_id, now, next_safety_sync_at)`. A stale completion raises a retryable synchronization error so transport negative-acknowledges. `AuthorizationRevoked` marks reauthorization required and returns its terminal result without moving the cursor. Let `HistoryCursorExpired` propagate to the recovery service added in Task 8.

- [ ] **Step 5: Run tests and commit**

Run:

```bash
uv run pytest tests/unit/connectors/gmail/test_sync.py tests/integration/connectors/test_gmail_ingestion.py -v
uv run ruff check src/eva_ai/connectors/gmail/sync.py tests/unit/connectors/gmail/test_sync.py tests/integration/connectors/test_gmail_ingestion.py
uv run mypy src/eva_ai/connectors/gmail/sync.py
```

Expected: all commands pass.

Commit:

```bash
git add src/eva_ai/connectors/gmail/sync.py tests/unit/connectors/gmail/test_sync.py tests/integration/connectors/test_gmail_ingestion.py
git commit -m "feat: synchronize Gmail history"
```

---

### Task 8: Watch Renewal, Safety Sync, and Expired-History Recovery

**Files:**
- Create: `src/eva_ai/connectors/gmail/maintenance.py`
- Modify: `src/eva_ai/connectors/gmail/sync.py`
- Test: `tests/unit/connectors/gmail/test_maintenance.py`
- Modify: `tests/unit/connectors/gmail/test_sync.py`

**Interfaces:**
- Consumes: persisted due timestamps, repository claims, Gmail `watch`, `list_message_ids`, ordinary normalization/Event ingestion.
- Produces: `GmailMaintenanceService.run_due(now) -> MaintenanceSummary` and `GmailRecoveryService.recover(claim, gmail, now) -> SyncResult`.

`MaintenanceSummary` is a frozen dataclass with integer `renewed`, `safety_synced`, and `failed` counts.

- [ ] **Step 1: Write failing renewal and due-work tests**

Test renewal at 24 hours, no early renewal, worker-startup due work, safety sync after 60 minutes of notification silence, one active lease across maintenance and notification work, renewal preserving the old history cursor, and renewal recording only expiration/next due time.

- [ ] **Step 2: Write failing 404 recovery tests**

Fake `HistoryCursorExpired`, use a fixed `connected_at` whose epoch is `1788064200`, return two `users.messages.list` pages for query `in:inbox after:1788064200`, include one pre-connection result, and assert exact internalDate filtering, idempotent Event ingestion, a fresh watch, and final cursor replacement only after every recovery Event is durable.

- [ ] **Step 3: Run maintenance tests and verify RED**

Run: `uv run pytest tests/unit/connectors/gmail/test_maintenance.py tests/unit/connectors/gmail/test_sync.py -v`

Expected: FAIL because maintenance and recovery services do not exist.

- [ ] **Step 4: Implement persisted due-work execution**

Load only active due connectors. Renewal acquires a synchronization claim, loads credentials, asynchronously constructs the Gmail client, calls `watch`, then calls claim-protected `record_watch_renewal()` with expiration and `min(now + 24 hours, expiration - 24 hours)`; discard the watch response history ID. Safety work invokes `sync_connector()`, which acquires its own claim from the persisted cursor and schedules the next check at `now + 60 minutes`.

- [ ] **Step 5: Implement bounded expired-history recovery**

Keep the existing sync claim. Page through `list_message_ids(query=f"in:inbox after:{int(connected_at.timestamp())}")`, deduplicate, fetch, recheck `INBOX` and exact `internalDate >= connected_at`, normalize, and ingest. Call a fresh watch only after all Events are durable, then claim-protected-complete with the new watch history ID. Any failure leaves the old cursor retryable.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
uv run pytest tests/unit/connectors/gmail/test_maintenance.py tests/unit/connectors/gmail/test_sync.py -v
uv run ruff check src/eva_ai/connectors/gmail/maintenance.py src/eva_ai/connectors/gmail/sync.py tests/unit/connectors/gmail
uv run mypy src/eva_ai/connectors/gmail/maintenance.py src/eva_ai/connectors/gmail/sync.py
```

Expected: all commands pass.

Commit:

```bash
git add src/eva_ai/connectors/gmail/maintenance.py src/eva_ai/connectors/gmail/sync.py tests/unit/connectors/gmail
git commit -m "feat: repair Gmail watch and history gaps"
```

---

### Task 9: Pub/Sub Pull Worker and Safe Operational Logging

**Files:**
- Create: `src/eva_ai/connectors/gmail/worker.py`
- Modify: `src/eva_ai/logging.py`
- Test: `tests/unit/connectors/gmail/test_worker.py`
- Modify: `tests/unit/test_logging.py`

**Interfaces:**
- Consumes: `PullSubscriber`, `decode_notification`, `GmailSyncService`, `GmailMaintenanceService`.
- Produces: `GmailPullWorker.run_once(max_messages=10) -> PullBatchResult` and `run_forever() -> None`.

`PullBatchResult` is a frozen dataclass with integer `pulled`, `acknowledged`, and `negative_acknowledged` counts.

- [ ] **Step 1: Write failing acknowledgement matrix tests**

Assert ACK for successful, duplicate/already-covered, unknown-account, malformed, and reauthorization-required outcomes; NACK for `CONNECTING`, busy/stale-claim, provider transient, database, and unexpected internal failures. Verify one message failure does not prevent decisions for other pulled messages.

- [ ] **Step 2: Write failing log-redaction tests**

Log a fake failure containing a subject, recipient, refresh token, and body. Assert output contains only connector ID, workspace ID, Pub/Sub message ID, Gmail message/thread ID, claim ID, operation, outcome, and fixed error category; assert all supplied sensitive strings are absent.

- [ ] **Step 3: Run worker/log tests and verify RED**

Run: `uv run pytest tests/unit/connectors/gmail/test_worker.py tests/unit/test_logging.py -v`

Expected: collection or assertions fail because worker and Gmail-safe fields do not exist.

- [ ] **Step 4: Implement one bounded pull batch**

Pull at most `max_messages`, process sequentially, collect ACK and NACK IDs, then call each transport operation once per nonempty group. Decoder failures never pass raw data into logs. Invoke `maintenance.run_due(clock.now())` after the batch, including empty deadline batches.

- [ ] **Step 5: Implement continuous local orchestration**

`run_forever()` repeatedly calls `run_once()`; the Pub/Sub RPC timeout provides the bounded wake-up interval, so no long sleep owns correctness. In `finally`, await `subscriber.close()`; on cancellation, let `CancelledError` propagate after cleanup. Do not add a public HTTP endpoint.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
uv run pytest tests/unit/connectors/gmail/test_worker.py tests/unit/test_logging.py -v
uv run ruff check src/eva_ai/connectors/gmail/worker.py src/eva_ai/logging.py tests/unit/connectors/gmail/test_worker.py tests/unit/test_logging.py
uv run mypy src/eva_ai/connectors/gmail/worker.py src/eva_ai/logging.py
```

Expected: all commands pass.

Commit:

```bash
git add src/eva_ai/connectors/gmail/worker.py src/eva_ai/logging.py tests/unit/connectors/gmail/test_worker.py tests/unit/test_logging.py
git commit -m "feat: consume Gmail Pub/Sub notifications"
```

---

### Task 10: CLI Composition, Local Scope Bootstrap, and Operator Documentation

**Files:**
- Create: `src/eva_ai/local_scope.py`
- Create: `src/eva_ai/cli.py`
- Create: `docs/gmail-setup.md`
- Modify: `.gitignore`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `Makefile`
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: all prior services/adapters and `Settings`.
- Produces: `eva scope create`, `eva gmail connect`, `eva gmail sync`, `eva gmail pull`, and `eva gmail maintain`; Make targets `gmail-connect`, `gmail-sync`, `gmail-pull`, and `gmail-maintain`.

- [ ] **Step 1: Write failing CLI parser and composition tests**

Assert exact argument requirements and exit behavior:

```text
eva scope create --display-name "Saswat Ray" --workspace-name personal
eva gmail connect --user-id 0191cafe-7b00-7000-8000-000000000001 --workspace-id 0191cafe-7b00-7000-8000-000000000002
eva gmail sync --connector-id 0191cafe-7b00-7000-8000-000000000003
eva gmail pull
eva gmail maintain
```

Use injected command functions so tests never open a browser or construct real Google clients. Assert `gmail connect` refuses missing project ID, Gmail account, OAuth client file, user ID, or workspace ID with a content-free error.

- [ ] **Step 2: Run CLI tests and verify RED**

Run: `uv run pytest tests/unit/test_cli.py -v`

Expected: collection fails because `eva_ai.cli` does not exist.

- [ ] **Step 3: Implement local scope and CLI composition**

`scope create` creates one explicit User and Workspace in a transaction and prints only their UUIDs. `gmail connect` builds OAuth, Gmail, Secret Manager, repository, and bootstrap services and prints the ConnectorAccount UUID. `gmail sync` performs one synchronization from the stored durable cursor for the selected connector, making deterministic replay testing possible. `gmail pull` builds the pull worker and runs continuously. `gmail maintain` executes one due-maintenance pass and exits. Use `argparse` plus `asyncio.run`; keep dependency constructors separately testable.

- [ ] **Step 4: Protect local OAuth material**

Add these ignore rules:

```gitignore
.secrets/
client_secret*.json
```

Add non-secret `.env.example` values:

```dotenv
EVA_PUBSUB_PROJECT_ID=evaai-507018
EVA_GMAIL_TOPIC_ID=eva-gmail-notifications
EVA_GMAIL_SUBSCRIPTION_ID=eva-gmail-ingestion-local
EVA_GMAIL_ACCOUNT=saswatray2505@gmail.com
EVA_GMAIL_OAUTH_CLIENT_FILE=.secrets/google-oauth-client.json
```

- [ ] **Step 5: Document idempotent GCP setup and manual OAuth checkpoint**

Document and later execute:

```bash
gcloud services enable gmail.googleapis.com pubsub.googleapis.com secretmanager.googleapis.com --project=evaai-507018
gcloud pubsub topics create eva-gmail-notifications --project=evaai-507018
gcloud pubsub topics add-iam-policy-binding eva-gmail-notifications --project=evaai-507018 --member=serviceAccount:gmail-api-push@system.gserviceaccount.com --role=roles/pubsub.publisher
gcloud pubsub subscriptions create eva-gmail-ingestion-local --project=evaai-507018 --topic=eva-gmail-notifications
gcloud auth application-default login
```

The guide must instruct the user to configure Google Auth Platform as External, set publishing status to In production, declare only `gmail.readonly`, create a Desktop OAuth client, download it to `.secrets/google-oauth-client.json`, and pass the unverified personal-use warning. Include commands for migration, scope creation, connection, pull worker, database Event inspection, recovery, and explicit teardown. Teardown commands are documentation only and are not executed during milestone work.

- [ ] **Step 6: Run CLI/docs checks and commit**

Run:

```bash
uv run pytest tests/unit/test_cli.py -v
uv run eva --help
uv run eva gmail --help
uv run ruff check src/eva_ai/cli.py src/eva_ai/local_scope.py tests/unit/test_cli.py
uv run mypy src/eva_ai/cli.py src/eva_ai/local_scope.py
git diff --check
```

Expected: all commands pass and both help commands exit 0 without Google credentials.

Commit:

```bash
git add src/eva_ai/cli.py src/eva_ai/local_scope.py tests/unit/test_cli.py docs/gmail-setup.md .gitignore .env.example README.md Makefile pyproject.toml
git commit -m "docs: add Gmail ingestion operations"
```

---

### Task 11: Full Verification, Live Gmail Smoke Test, Review, Push, and PR

**Files:**
- Modify only files required by verified defects found in this task.

**Interfaces:**
- Consumes: complete Milestone 2 branch, real GCP project, manual OAuth desktop-client file, personal Gmail mailbox.
- Produces: verified local ingestion, clean branch, pushed remote branch, and GitHub pull request.

- [ ] **Step 1: Run the full automated verification gate**

Run:

```bash
make db-up
make verify
uv run alembic downgrade 20260830_0002
uv run alembic upgrade head
git diff --check
```

Expected: Ruff format/lint, strict mypy, all unit/integration tests, and migration round trip pass with zero failures.

- [ ] **Step 2: Perform a requirements audit against the design**

Check each Acceptance Criteria bullet in `docs/superpowers/specs/2026-08-30-milestone-2-gmail-ingestion-design.md` against a named automated test or live-smoke step. Record any gap as a failing test before changing production code.

- [ ] **Step 3: Provision idempotent GCP resources**

Run the documented service-enable, topic-create-if-absent, topic-IAM, and subscription-create-if-absent commands. Verify:

```bash
gcloud pubsub topics get-iam-policy eva-gmail-notifications --project=evaai-507018
gcloud pubsub subscriptions describe eva-gmail-ingestion-local --project=evaai-507018
gcloud services list --enabled --project=evaai-507018 --filter='name:(gmail.googleapis.com OR pubsub.googleapis.com OR secretmanager.googleapis.com)'
```

Expected: Gmail push identity has topic-level publisher, the pull subscription targets the approved topic, and all three APIs are enabled.

- [ ] **Step 4: Pause for the user's OAuth Console artifact**

Ask the user to place the downloaded Desktop OAuth client at `.secrets/google-oauth-client.json`. Verify only file existence and Git ignored status:

```bash
test -f .secrets/google-oauth-client.json
git check-ignore .secrets/google-oauth-client.json
```

Never print, parse to terminal, commit, or transmit its contents.

- [ ] **Step 5: Connect the mailbox and start ingestion**

Run migrations, create the explicit local User/Workspace, put their UUIDs into the connect command, complete browser consent, and start `eva gmail pull`. Confirm ConnectorAccount is `ACTIVE`, Secret Manager contains a secret version, GmailSyncState has cursor/expiration, and PostgreSQL contains no credential values.

- [ ] **Step 6: Execute the live message matrix**

Send after connection:

1. one plain-text inbox email
2. one HTML inbox email
3. one inbox email with a small attachment
4. one Promotions/Social/Updates-category email when available

For each Gmail message ID, verify one Event and one Outbox row, schema version 1, correct Gmail-thread correlation key, labels, bodies, and attachment metadata. Confirm no attachment binary is stored.

- [ ] **Step 7: Verify replay and restart behavior**

Run `uv run eva gmail sync --connector-id "$EVA_GMAIL_CONNECTOR_ID"` twice, then restart the pull worker and send one more email. Assert Event and Outbox counts remain one per Gmail message and the cursor advances after restart.

- [ ] **Step 8: Inspect logs and stored state for secrets/content leakage**

Search captured logs and database connector/error fields for the known test subject/body marker, recipient address, OAuth filename, `refresh_token`, `access_token`, and `client_secret`. Expected: none occur outside the canonical Event payload fields explicitly approved for email content.

- [ ] **Step 9: Request code review and address only verified findings**

Use `superpowers:requesting-code-review`. For every finding, use `superpowers:receiving-code-review`, reproduce the issue, add or adjust a failing test, implement the smallest fix, and rerun the focused plus full verification gates.

- [ ] **Step 10: Commit any final verified fixes**

```bash
git status --short
make verify
```

When verified fixes changed files, stage only the exact paths shown by `git status --short`, commit them as `fix: harden Gmail ingestion`, and rerun `make verify`. Skip the commit when no files changed. Expected final state: clean working tree and complete passing verification output.

- [ ] **Step 11: Push and create the pull request**

```bash
git push -u origin codex/milestone-2-gmail-ingestion
gh pr create --base main --head codex/milestone-2-gmail-ingestion --title "Milestone 2: Gmail ingestion" --body-file /tmp/eva-milestone-2-pr.md
gh pr checks --watch
```

The PR body must summarize architecture, OAuth/GCP manual setup, migrations, test counts, live-smoke evidence, security boundaries, and deferred capabilities. Do not merge the PR without the user's explicit instruction.
