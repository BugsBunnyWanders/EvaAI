# Milestone 8 Gmail Actions and Approval Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `subagent-driven-development` when the user selects
> delegated execution, or `executing-plans` when the user selects inline execution. Follow this
> plan task-by-task and stop at every review checkpoint.

**Goal:** Give Eva safe Gmail draft, revision, discard, and exact-approval send capabilities for
both proactive AgentRuns and reactive Telegram conversations.

**Architecture:** Keep OpenAI adapters read-only. Structured model proposals enter a deterministic
action application layer, which validates tenant scope, canonicalizes the email, evaluates a closed
policy registry, and persists immutable proposals/actions with transactional Outbox messages. The
existing relay publishes execution requests, a worker converts them into deterministic Cloud Tasks,
and a private Cloud Run executor performs scoped Gmail mutations outside database transactions.
Telegram approval callbacks bind one paired user to one immutable content hash and one 24-hour
approval window.

**Tech Stack:** Python 3.14, FastAPI, Pydantic v2, SQLAlchemy 2 async, PostgreSQL, Alembic,
Google Gmail API, Google Pub/Sub, Google Cloud Tasks, Cloud Run, Terraform, GitHub Actions, pytest,
Ruff, mypy.

**Spec:**
[`docs/superpowers/specs/2026-09-17-milestone-8-actions-approval-design.md`](../specs/2026-09-17-milestone-8-actions-approval-design.md)

## Global Constraints

- The model may propose actions but never receives a Gmail mutation tool.
- Only `gmail.create_draft`, `gmail.update_draft`, `gmail.send_draft`, and
  `gmail.delete_draft` are registered capabilities; unknown capabilities are denied.
- Creating/updating/deleting an Eva-managed draft is deterministic-policy `ALLOW`; sending is
  always `REQUIRE_APPROVAL`.
- Approval is bound to one immutable proposal version, paired Telegram principal, exact 24-hour
  expiry, and SHA-256 hash of every send-affecting field.
- Attachments are fixed to an empty tuple and any non-empty attachment input is rejected.
- Every repository query and foreign-key relationship must preserve `user_id` and `workspace_id`.
- No Pub/Sub, Cloud Tasks, Gmail, Telegram, or OpenAI call may occur inside a database transaction.
- Commit `provider_call_started_at` before Gmail mutation. An expired lease after that boundary and
  before a result becomes `UNKNOWN`; do not automatically repeat the provider call.
- Raw OAuth values, callback tokens, message bodies, full recipient lists, and provider responses
  must not appear in logs, task payloads, Pub/Sub attributes, or action-result metadata.
- Add comments for non-obvious authorization, exact-hash, transaction, provider-boundary,
  idempotency, and tenant-scope invariants.
- Follow test-driven development: write the focused failing test, run it to see the expected
  failure, implement the smallest behavior, run it green, then commit.

## File Map

### New application modules

- `src/eva_ai/actions/types.py` — immutable domain records, state enums, event/task contracts.
- `src/eva_ai/actions/contracts.py` — repository, task-enqueuer, executor, and proposal ports.
- `src/eva_ai/actions/errors.py` — sanitized validation, scope, conflict, and provider errors.
- `src/eva_ai/actions/canonical.py` — normalized email contract and deterministic content hash.
- `src/eva_ai/actions/policy.py` — closed capability registry and deterministic policy engine.
- `src/eva_ai/actions/repository.py` — proposal, approval, action, managed-draft, and revision state.
- `src/eva_ai/actions/service.py` — proposal intake, approval decisions, revision, and dispatch rules.
- `src/eva_ai/actions/dispatcher.py` — Pub/Sub execution-request to Cloud Tasks handoff.
- `src/eva_ai/actions/executor.py` — claim, revalidation, Gmail call, and durable completion.
- `src/eva_ai/actions/api.py` — private Cloud Tasks HTTP entry point only.
- `src/eva_ai/connectors/gmail/actions.py` — scoped Gmail write application adapter.
- `src/eva_ai/connectors/gmail/mime.py` — RFC 5322/MIME construction for new mail and replies.
- `src/eva_ai/integrations/gcp/tasks.py` — Google Cloud Tasks adapter.

### New persistence

- `src/eva_ai/db/models/actions.py` — action-domain SQLAlchemy models.
- `migrations/versions/20260917_0010_actions_approval.py` — tables, constraints, indexes, and
  notification reply markup.

### Existing seams to modify

- `src/eva_ai/agent/repository.py` and `src/eva_ai/agent/service.py` — persist proactive action
  proposals atomically with successful runs.
- `src/eva_ai/conversation/repository.py`, `service.py`, and `worker.py` — persist reactive
  proposals and route revision text/callbacks.
- `src/eva_ai/connectors/gmail/contracts.py`, `bootstrap.py`,
  `src/eva_ai/integrations/gmail/api.py`, and `oauth.py` — compose scope and Gmail mutations.
- `src/eva_ai/notifications/types.py`, `repository.py`, `service.py`, and `worker.py` — optional
  Telegram inline keyboards.
- `src/eva_ai/integrations/telegram/api.py`, `src/eva_ai/telegram/types.py`, and `webhook.py` —
  button delivery and callback validation.
- `src/eva_ai/config.py`, `worker.py`, `cli.py`, and `main.py` — runtime configuration and wiring.
- `terraform/environments/production/{versions,locals,variables,pubsub,identity,cloud_run,outputs}.tf`
  — Cloud Tasks queue, private executor, IAM, subscription, and environment.
- `.github/workflows/deploy.yml` — deploy executor and verify the private endpoint configuration.
- `pyproject.toml` and `uv.lock` — add the Cloud Tasks client.
- `README.md` and `.env.example` — operator setup and controlled smoke instructions.

---

## Task 1: Define Canonical Email, Action Contracts, and Deterministic Policy

**Files:**

- Create: `src/eva_ai/actions/__init__.py`
- Create: `src/eva_ai/actions/errors.py`
- Create: `src/eva_ai/actions/types.py`
- Create: `src/eva_ai/actions/canonical.py`
- Create: `src/eva_ai/actions/policy.py`
- Test: `tests/unit/actions/test_canonical.py`
- Test: `tests/unit/actions/test_policy.py`
- Test: `tests/unit/actions/test_types.py`

### Step 1: Write failing canonicalization and policy tests

Cover these cases explicitly:

- address casing/whitespace normalize deterministically;
- recipient order and duplicates normalize without changing delivery semantics;
- `subject`, `text_body`, `html_body`, reply context, and empty attachments all affect the hash;
- non-empty attachments fail validation;
- create/update/delete return `ALLOW` only with their capability-specific trusted context;
- send always returns `REQUIRE_APPROVAL`;
- an unknown capability cannot be constructed or evaluated.

Core contract:

```python
class GmailActionCapability(StrEnum):
    CREATE_DRAFT = "gmail.create_draft"
    UPDATE_DRAFT = "gmail.update_draft"
    SEND_DRAFT = "gmail.send_draft"
    DELETE_DRAFT = "gmail.delete_draft"


class CanonicalEmail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["NEW", "REPLY"]
    to: tuple[str, ...]
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    subject: str
    text_body: str
    html_body: str | None = None
    thread_id: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()


def canonical_email_hash(message: CanonicalEmail) -> str:
    payload = message.model_dump_json(exclude_none=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

The implementation must use a stable JSON representation with sorted object keys; do not rely on
Pydantic field order alone. A field validator rejects any non-empty `attachments` tuple.

### Step 2: Run the focused tests and confirm failure

Run:

```bash
uv run pytest tests/unit/actions/test_canonical.py tests/unit/actions/test_policy.py \
  tests/unit/actions/test_types.py -q
```

Expected: collection fails because `eva_ai.actions` does not exist.

### Step 3: Implement the minimal domain layer

Define these persisted state enums in `types.py`:

- `ActionOrigin`: `AGENT_RUN`, `TELEGRAM_USER`, `SYSTEM`
- `PolicyDecision`: `ALLOW`, `REQUIRE_APPROVAL`, `DENY`
- `ActionProposalStatus`: the statuses from section 5.1 of the spec
- `ApprovalStatus`: `PENDING`, `GRANTED`, `REJECTED`, `EXPIRED`, `SUPERSEDED`
- `ActionStatus`: `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `UNKNOWN`
- `ManagedDraftStatus`: `CREATING`, `READY`, `UPDATING`, `SENDING`, `SENT`, `DELETING`,
  `DELETED`, `UNKNOWN`

Define `ActionExecutionRequestedMessage` with `message_type="action.execution.requested"`, opaque
action/scope IDs, and schema version. Define `ActionTaskRequest` with only `action_id` and
`schema_version`; this is the entire Cloud Tasks body.

Implement `ActionPolicyEngine.evaluate(capability, context)` as exhaustive `match` logic. The
context must contain trusted booleans such as `scoped_connector`, `managed_draft`,
`authenticated_discard`, and `exact_approval`; it must not accept policy instructions from model
arguments.

### Step 4: Run tests, lint, and typecheck

Run:

```bash
uv run pytest tests/unit/actions/test_canonical.py tests/unit/actions/test_policy.py \
  tests/unit/actions/test_types.py -q
uv run ruff check src/eva_ai/actions tests/unit/actions
uv run mypy src/eva_ai/actions tests/unit/actions
```

Expected: all pass.

### Step 5: Commit

```bash
git add src/eva_ai/actions tests/unit/actions
git commit -m "feat: define safe action contracts and policy"
```

---

## Task 2: Add the Action Persistence Schema

**Files:**

- Create: `src/eva_ai/db/models/actions.py`
- Modify: `src/eva_ai/db/models/__init__.py`
- Modify: `src/eva_ai/db/models/notifications.py`
- Create: `migrations/versions/20260917_0010_actions_approval.py`
- Test: `tests/integration/actions/test_schema.py`
- Modify: `tests/integration/test_migrations.py`

### Step 1: Write failing schema tests

Assert that migrations create:

- `action_proposals`
- `action_approvals`
- `actions`
- `action_results`
- `managed_gmail_drafts`
- `action_revision_sessions`
- nullable JSONB `notifications.reply_markup`

Exercise composite tenant foreign keys, immutable proposal uniqueness, one action per idempotency
key, one active send proposal per managed draft, one callback digest per approval, and only one
active revision session per paired Telegram account.

### Step 2: Run the tests and confirm failure

Run:

```bash
uv run pytest tests/integration/actions/test_schema.py tests/integration/test_migrations.py -q
```

Expected: schema assertions fail because migration `0010` is absent.

### Step 3: Implement models and migration

Every table includes `user_id` and `workspace_id`; every relationship to another action table uses
a composite foreign key including both. Use database enums already established by the project,
JSONB only for immutable canonical parameters/sanitized metadata, and check constraints for:

```text
expires_at > created_at
version >= 1
attempt_count >= 0
provider_call_started_at IS NULL OR started_at IS NOT NULL
finished_at IS NULL OR started_at IS NOT NULL
```

`action_revision_sessions` stores the Telegram account, conversation, managed draft, active send
proposal, status, created/expiry/completion timestamps, but no natural-language revision text.

### Step 4: Run migration tests and inspect both directions

Run:

```bash
uv run alembic upgrade head
uv run pytest tests/integration/actions/test_schema.py tests/integration/test_migrations.py -q
uv run alembic downgrade 20260907_0009
uv run alembic upgrade head
```

Expected: upgrade, focused tests, downgrade, and re-upgrade succeed.

### Step 5: Commit

```bash
git add src/eva_ai/db/models migrations/versions tests/integration/actions \
  tests/integration/test_migrations.py
git commit -m "feat: add action approval persistence"
```

---

## Task 3: Implement Repository State Machines and Transactional Outbox

**Files:**

- Create: `src/eva_ai/actions/contracts.py`
- Create: `src/eva_ai/actions/repository.py`
- Test: `tests/integration/actions/test_repository.py`
- Test: `tests/unit/actions/test_repository_contract.py`

### Step 1: Write failing repository tests

Tests must prove:

- proposal + action + Outbox commit atomically;
- duplicate source/idempotency keys return the original rows;
- old/superseded/expired approvals cannot queue sends;
- a callback replay returns the existing decision/action;
- create success persists a managed draft and a separate send proposal in one transaction;
- update success supersedes the old send proposal and creates a new send proposal/version;
- discard can target only the exact scoped managed draft;
- a cross-Workspace ID mix matches no row;
- an expired RUNNING action before `provider_call_started_at` can be reclaimed;
- an expired RUNNING action after `provider_call_started_at` becomes `UNKNOWN` instead of reclaimed.

### Step 2: Run tests and confirm failure

Run:

```bash
uv run pytest tests/integration/actions/test_repository.py \
  tests/unit/actions/test_repository_contract.py -q
```

Expected: imports or repository assertions fail.

### Step 3: Implement repository commands

Expose explicit methods rather than a generic update API:

```python
async def create_allowed_action_in_session(
    session: AsyncSession,
    *,
    proposal: NewActionProposal,
    destination: str,
    now: datetime,
) -> tuple[ActionProposalRecord, ActionRecord]: ...

async def create_send_approval_in_session(
    session: AsyncSession,
    *,
    managed_draft: ManagedGmailDraftRecord,
    proposal: NewActionProposal,
    callback_token_digest: str,
    expires_at: datetime,
    destination: str,
    now: datetime,
) -> tuple[ActionProposalRecord, ApprovalRecord]: ...

async def grant_and_queue_send(
    *,
    callback_token_digest: str,
    telegram_account_id: UUID,
    chat_id: int,
    destination: str,
    now: datetime,
) -> ApprovalDecision: ...

async def claim_action(
    request: ActionTaskRequest,
    *,
    now: datetime,
    lease_seconds: int,
) -> ActionClaimResult: ...

async def mark_provider_call_started(
    claim: ActionClaim,
    *,
    started_at: datetime,
) -> bool: ...
```

Use `SELECT ... FOR UPDATE` for approval, managed-draft, and revision transitions. Construct Outbox
IDs and action idempotency keys deterministically from proposal/action IDs. Never mutate
`parameters_json`, `parameters_hash`, proposal version, or approval hash.

### Step 4: Run focused verification

Run:

```bash
uv run pytest tests/integration/actions/test_repository.py \
  tests/unit/actions/test_repository_contract.py -q
uv run ruff check src/eva_ai/actions tests/unit/actions tests/integration/actions
uv run mypy src/eva_ai/actions tests/unit/actions
```

Expected: all pass.

### Step 5: Commit

```bash
git add src/eva_ai/actions tests/unit/actions tests/integration/actions
git commit -m "feat: persist idempotent action transitions"
```

---

## Task 4: Add Gmail Compose Scope, MIME Construction, and Write Adapter

**Files:**

- Modify: `src/eva_ai/connectors/gmail/contracts.py`
- Modify: `src/eva_ai/connectors/gmail/bootstrap.py`
- Create: `src/eva_ai/connectors/gmail/mime.py`
- Create: `src/eva_ai/connectors/gmail/actions.py`
- Modify: `src/eva_ai/integrations/gmail/oauth.py`
- Modify: `src/eva_ai/integrations/gmail/api.py`
- Test: `tests/unit/connectors/gmail/test_mime.py`
- Test: `tests/unit/connectors/gmail/test_actions.py`
- Modify: `tests/unit/connectors/gmail/test_bootstrap.py`
- Modify: `tests/unit/integrations/gmail/test_api.py`
- Modify: `tests/unit/integrations/gmail/test_oauth.py`

### Step 1: Write failing tests

Test:

- new message and reply MIME headers/body alternatives;
- deterministic `Message-ID` supplied by the application;
- reply `In-Reply-To`, `References`, matching subject, and Gmail `threadId`;
- Bcc recipients appear in the pre-provider RFC message so Gmail can deliver to them; the test does
  not expect Gmail's post-send copy to retain the Bcc header;
- create/update/delete/send invoke only `users.drafts.*` endpoints;
- provider errors are sanitized and classified;
- credential construction requires `gmail.readonly` plus `gmail.compose` for action clients;
- ingestion-only use remains available when compose is missing, while action creation returns a
  reauthorization-required result.

### Step 2: Run tests and confirm failure

Run:

```bash
uv run pytest tests/unit/connectors/gmail/test_mime.py \
  tests/unit/connectors/gmail/test_actions.py \
  tests/unit/connectors/gmail/test_bootstrap.py \
  tests/unit/integrations/gmail/test_api.py \
  tests/unit/integrations/gmail/test_oauth.py -q
```

Expected: new modules/methods are missing.

### Step 3: Implement contracts and adapters

Add:

```python
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_CONNECTOR_SCOPES = (GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE)

class GmailActionClient(Protocol):
    async def create_draft(self, raw: str, thread_id: str | None) -> GmailDraftResult: ...
    async def update_draft(
        self, draft_id: str, raw: str, thread_id: str | None
    ) -> GmailDraftResult: ...
    async def delete_draft(self, draft_id: str) -> None: ...
    async def send_draft(self, draft_id: str) -> GmailSendResult: ...
    async def get_draft(self, draft_id: str) -> Mapping[str, object]: ...
    async def close(self) -> None: ...
```

Use `EmailMessage`, UTF-8, URL-safe base64 without logging the encoded message, and the Gmail draft
resource. Keep `messages.send` outside the protocol and implementation.

### Step 4: Run focused verification

Run the Task 4 test command, then:

```bash
uv run ruff check src/eva_ai/connectors/gmail src/eva_ai/integrations/gmail \
  tests/unit/connectors/gmail tests/unit/integrations/gmail
uv run mypy src/eva_ai/connectors/gmail src/eva_ai/integrations/gmail
```

Expected: all pass.

### Step 5: Commit

```bash
git add src/eva_ai/connectors/gmail src/eva_ai/integrations/gmail \
  tests/unit/connectors/gmail tests/unit/integrations/gmail
git commit -m "feat: add scoped Gmail draft operations"
```

---

## Task 5: Ingest Proactive and Reactive Draft Proposals

**Files:**

- Modify: `src/eva_ai/agent/types.py`
- Modify: `src/eva_ai/agent/repository.py`
- Modify: `src/eva_ai/agent/service.py`
- Modify: `src/eva_ai/conversation/repository.py`
- Modify: `src/eva_ai/conversation/service.py`
- Create: `src/eva_ai/actions/recipients.py`
- Modify: `src/eva_ai/actions/service.py`
- Test: `tests/unit/actions/test_recipients.py`
- Test: `tests/unit/actions/test_service.py`
- Modify: `tests/unit/agent/test_service.py`
- Modify: `tests/integration/agent/test_repository.py`
- Modify: `tests/unit/conversation/test_service.py`
- Modify: `tests/integration/telegram/test_conversation_flow.py`

### Step 1: Write failing tests

Cover:

- an explicit valid address creates a proposal;
- one bounded Gmail-history match resolves a named person;
- zero/multiple plausible matches return clarification and create no proposal;
- model-supplied scope/connector/provenance is ignored;
- proactive and reactive completion persist action intent without losing the successful run/turn;
- unsupported capability, attachment, cross-scope thread, or malformed message creates no action;
- redelivery cannot duplicate a proposal/action.

### Step 2: Run tests and confirm failure

Run:

```bash
uv run pytest tests/unit/actions/test_recipients.py tests/unit/actions/test_service.py \
  tests/unit/agent/test_service.py tests/integration/agent/test_repository.py \
  tests/unit/conversation/test_service.py \
  tests/integration/telegram/test_conversation_flow.py -q
```

Expected: proposal intake assertions fail.

### Step 3: Implement transactional intake

Change the completion repositories to accept a narrow `ActionProposalWriter` and call its
`create_from_model_proposals_in_session(...)` method inside the same database transaction that
marks the AgentRun or conversation turn successful. The writer receives trusted IDs from the
repository and the model proposal only for capability/description/message content.

For recipient names, `ActionProposalService` first returns a clarification result. The existing
read-only Gmail tool path performs a bounded search, and only structured candidate addresses are
fed back into intake. Never query another connector and never guess among multiple candidates.

### Step 4: Run focused verification

Run the Task 5 test command and:

```bash
uv run ruff check src/eva_ai/actions src/eva_ai/agent src/eva_ai/conversation \
  tests/unit/actions tests/unit/agent tests/unit/conversation
uv run mypy src/eva_ai/actions src/eva_ai/agent src/eva_ai/conversation
```

Expected: all pass.

### Step 5: Commit

```bash
git add src/eva_ai/actions src/eva_ai/agent src/eva_ai/conversation \
  tests/unit/actions tests/unit/agent tests/unit/conversation \
  tests/integration/agent tests/integration/telegram
git commit -m "feat: ingest proactive and reactive email drafts"
```

---

## Task 6: Add Idempotent Pub/Sub-to-Cloud-Tasks Dispatch

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/eva_ai/integrations/gcp/tasks.py`
- Create: `src/eva_ai/actions/dispatcher.py`
- Test: `tests/unit/integrations/gcp/test_tasks.py`
- Test: `tests/unit/actions/test_dispatcher.py`

### Step 1: Add dependency and write failing tests

Run:

```bash
uv add 'google-cloud-tasks>=2,<3'
```

Then test that:

- task body contains only action ID/schema version;
- task name is a deterministic hash of action ID and capability;
- HTTP method, OIDC service account, and audience are exact;
- `AlreadyExists` is success;
- Pub/Sub is acknowledged only after enqueue success/AlreadyExists;
- malformed, wrong-message-type, and scope-invalid messages are safely classified;
- transient enqueue failure negative-acknowledges the Pub/Sub message.

### Step 2: Run tests and confirm failure

Run:

```bash
uv run pytest tests/unit/integrations/gcp/test_tasks.py \
  tests/unit/actions/test_dispatcher.py -q
```

Expected: task adapter and dispatcher imports fail.

### Step 3: Implement the adapter and worker

`GoogleCloudTaskEnqueuer.enqueue(request)` builds the queue path and deterministic task name, then
calls `CloudTasksAsyncClient.create_task`. `ActionDispatchPullWorker` validates
`ActionExecutionRequestedMessage`, reloads the action by tenant scope, and enqueues the opaque
`ActionTaskRequest`.

The Pub/Sub subscription uses the publisher's existing `message_type` attribute filter:

```text
attributes.message_type = "action.execution.requested"
```

### Step 4: Run focused verification

Run the Task 6 test command and:

```bash
uv run ruff check src/eva_ai/integrations/gcp src/eva_ai/actions tests/unit/integrations/gcp \
  tests/unit/actions
uv run mypy src/eva_ai/integrations/gcp src/eva_ai/actions
```

Expected: all pass.

### Step 5: Commit

```bash
git add pyproject.toml uv.lock src/eva_ai/integrations/gcp src/eva_ai/actions \
  tests/unit/integrations/gcp tests/unit/actions
git commit -m "feat: dispatch actions through Cloud Tasks"
```

---

## Task 7: Build the Private Action Executor

**Files:**

- Create: `src/eva_ai/actions/executor.py`
- Create: `src/eva_ai/actions/api.py`
- Test: `tests/unit/actions/test_executor.py`
- Test: `tests/unit/actions/test_api.py`
- Test: `tests/integration/actions/test_executor.py`

### Step 1: Write failing executor tests

Test create, update, delete, and send with a fake Gmail action client. Include:

- policy/scope/hash revalidation immediately before provider call;
- create success makes a managed draft and pending send approval;
- update success makes a fresh send proposal/version;
- send requires a current grant and exact provider draft content;
- callback or task replay returns the existing terminal result;
- known pre-provider transient failure is retryable;
- authorization revocation marks actions unavailable and preserves ingestion credentials/state;
- crash simulation after `provider_call_started_at` becomes `UNKNOWN` and does not call Gmail again;
- sanitized results exclude message content and recipient lists.

### Step 2: Run tests and confirm failure

Run:

```bash
uv run pytest tests/unit/actions/test_executor.py tests/unit/actions/test_api.py \
  tests/integration/actions/test_executor.py -q
```

Expected: executor/API imports fail.

### Step 3: Implement service and private endpoint

Use this provider boundary order:

```python
claim = await repository.claim_action(request, now=clock(), lease_seconds=lease_seconds)
subject = await repository.load_execution_subject(claim)
validated = validator.revalidate(subject)
await repository.mark_provider_call_started(claim, started_at=clock())
provider_result = await gmail_actions.execute(validated)
return await repository.complete(claim, provider_result=provider_result, completed_at=clock())
```

The route is `POST /internal/actions/execute`. It accepts `ActionTaskRequest`, returns 2xx for
terminal/idempotent/unknown outcomes, and returns a retryable status only for safe pre-provider or
classified safe-to-repeat failures. Do not add it to the public `eva_ai.main:app`.

### Step 4: Run focused verification

Run the Task 7 test command and:

```bash
uv run ruff check src/eva_ai/actions tests/unit/actions tests/integration/actions
uv run mypy src/eva_ai/actions
```

Expected: all pass.

### Step 5: Commit

```bash
git add src/eva_ai/actions tests/unit/actions tests/integration/actions
git commit -m "feat: execute Gmail actions behind private endpoint"
```

---

## Task 8: Deliver Exact Approval Cards and Process Send/Discard Callbacks

**Files:**

- Modify: `src/eva_ai/notifications/types.py`
- Modify: `src/eva_ai/notifications/repository.py`
- Modify: `src/eva_ai/notifications/service.py`
- Modify: `src/eva_ai/notifications/worker.py`
- Modify: `src/eva_ai/integrations/telegram/api.py`
- Modify: `src/eva_ai/telegram/types.py`
- Modify: `src/eva_ai/telegram/webhook.py`
- Modify: `src/eva_ai/conversation/service.py`
- Test: `tests/unit/integrations/test_telegram_api.py`
- Test: `tests/unit/notifications/test_service.py`
- Modify: `tests/unit/telegram/test_webhook.py`
- Test: `tests/integration/actions/test_approval_flow.py`

### Step 1: Write failing tests

Verify:

- notification delivery passes `reply_markup` to Telegram;
- the card shows mode, exact recipients, subject, complete body, expiry, and Send/Change/Discard;
- long bodies split safely, with the keyboard only on the final segment;
- callback data contains only operation prefix plus opaque random token;
- raw tokens are absent from persistence/logs and only SHA-256 digests are stored;
- paired Telegram account/private chat must match;
- Send grants once and queues one send action;
- Discard invalidates approval before queuing one exact delete action;
- expired/replayed/superseded/wrong-user callbacks do not invoke Gmail.

### Step 2: Run tests and confirm failure

Run:

```bash
uv run pytest tests/unit/integrations/test_telegram_api.py \
  tests/unit/notifications/test_service.py tests/unit/telegram/test_webhook.py \
  tests/integration/actions/test_approval_flow.py -q
```

Expected: reply markup and action callback assertions fail.

### Step 3: Implement notification and callback handling

Extend `TelegramBot.send_message` and `TelegramBotAPI.send_message` with optional validated inline
keyboard markup. The approval service generates one random token using `secrets.token_urlsafe`,
persists only its digest, and places the raw short token in callback data.

Callback processing must answer Telegram promptly, then perform the durable database transition.
The callback path delegates to `ActionService`; it must not contain policy or Gmail logic.

### Step 4: Run focused verification

Run the Task 8 test command and:

```bash
uv run ruff check src/eva_ai/notifications src/eva_ai/integrations/telegram \
  src/eva_ai/telegram src/eva_ai/conversation tests/unit/notifications \
  tests/unit/integrations/test_telegram_api.py tests/unit/telegram
uv run mypy src/eva_ai/notifications src/eva_ai/integrations/telegram src/eva_ai/telegram
```

Expected: all pass.

### Step 5: Commit

```bash
git add src/eva_ai/notifications src/eva_ai/integrations/telegram src/eva_ai/telegram \
  src/eva_ai/conversation tests/unit/notifications tests/unit/integrations/test_telegram_api.py \
  tests/unit/telegram tests/integration/actions
git commit -m "feat: add exact Telegram action approvals"
```

---

## Task 9: Implement Natural-Language Draft Revision

**Files:**

- Modify: `src/eva_ai/actions/service.py`
- Modify: `src/eva_ai/conversation/service.py`
- Modify: `src/eva_ai/conversation/worker.py`
- Modify: `src/eva_ai/integrations/openai/conversation.py`
- Test: `tests/unit/actions/test_revision.py`
- Modify: `tests/unit/conversation/test_service.py`
- Modify: `tests/unit/conversation/test_worker.py`
- Modify: `tests/unit/integrations/openai/test_conversation.py`
- Test: `tests/integration/actions/test_revision_flow.py`

### Step 1: Write failing tests

Test:

- Change supersedes the old approval immediately and opens bounded revision state;
- the next paired-user text receives current exact draft plus the revision instruction;
- the model returns a complete replacement message, never an executable patch;
- deterministic validation rejects attachments, missing recipients, and cross-thread changes;
- update preserves provider draft identity;
- successful update creates a new immutable send proposal and approval token;
- old callback token remains unusable;
- expired revision state falls back to normal conversation with a concise explanation.

### Step 2: Run tests and confirm failure

Run:

```bash
uv run pytest tests/unit/actions/test_revision.py tests/unit/conversation/test_service.py \
  tests/unit/conversation/test_worker.py \
  tests/unit/integrations/openai/test_conversation.py \
  tests/integration/actions/test_revision_flow.py -q
```

Expected: revision routing assertions fail.

### Step 3: Implement revision routing

At the start of authenticated text processing, query `ActionRevisionSession` by paired Telegram
account and scope. If active, run the dedicated structured revision adapter; otherwise use normal
conversation handling. Keep normal history/memory retrieval available for tone, but the exact
persisted draft is the only editable base.

Persist the update proposal/action and close the revision session atomically. After update execution
succeeds, issue the fresh send proposal/card through the Task 7 completion transaction.

### Step 4: Run focused verification

Run the Task 9 test command and:

```bash
uv run ruff check src/eva_ai/actions src/eva_ai/conversation \
  src/eva_ai/integrations/openai tests/unit/actions tests/unit/conversation \
  tests/unit/integrations/openai
uv run mypy src/eva_ai/actions src/eva_ai/conversation src/eva_ai/integrations/openai
```

Expected: all pass.

### Step 5: Commit

```bash
git add src/eva_ai/actions src/eva_ai/conversation src/eva_ai/integrations/openai \
  tests/unit/actions tests/unit/conversation tests/unit/integrations/openai \
  tests/integration/actions
git commit -m "feat: revise managed Gmail drafts safely"
```

---

## Task 10: Wire Runtime Configuration and Worker Lifecycle

**Files:**

- Modify: `src/eva_ai/config.py`
- Modify: `src/eva_ai/worker.py`
- Modify: `src/eva_ai/cli.py`
- Modify: `src/eva_ai/api/dependencies.py`
- Test: `tests/unit/test_config.py`
- Test: `tests/unit/test_worker.py`
- Test: `tests/unit/test_cli.py`

### Step 1: Write failing wiring tests

Assert validation for:

- action dispatch subscription;
- Cloud Tasks project/region/queue;
- executor URL/audience and caller service account;
- 24-hour approval expiry;
- revision expiry, action lease, task timeout, and bounded retries;
- dispatcher enabled only when every required setting exists;
- the shared worker TaskGroup starts and shuts down the action dispatcher cleanly;
- the private executor dependency graph does not expose Telegram webhook routes.

### Step 2: Run tests and confirm failure

Run:

```bash
uv run pytest tests/unit/test_config.py tests/unit/test_worker.py tests/unit/test_cli.py -q
```

Expected: action settings and worker assertions fail.

### Step 3: Implement settings and dependency builders

Add explicit `EVA_ACTIONS_ENABLED` gating and fail-fast validation when enabled. Keep the action
dispatcher in the existing worker pool. Build the executor app dependencies per Cloud Run request
process, sharing connection pools/clients via FastAPI lifespan and closing them on shutdown.

### Step 4: Run focused verification

Run the Task 10 test command and:

```bash
uv run ruff check src/eva_ai/config.py src/eva_ai/worker.py src/eva_ai/cli.py \
  src/eva_ai/api/dependencies.py tests/unit/test_config.py tests/unit/test_worker.py \
  tests/unit/test_cli.py
uv run mypy src/eva_ai
```

Expected: all pass.

### Step 5: Commit

```bash
git add src/eva_ai/config.py src/eva_ai/worker.py src/eva_ai/cli.py \
  src/eva_ai/api/dependencies.py tests/unit/test_config.py tests/unit/test_worker.py \
  tests/unit/test_cli.py
git commit -m "feat: wire action runtime services"
```

---

## Task 11: Provision Cloud Tasks and the Private Executor

**Files:**

- Modify: `terraform/environments/production/versions.tf`
- Modify: `terraform/environments/production/variables.tf`
- Modify: `terraform/environments/production/locals.tf`
- Modify: `terraform/environments/production/pubsub.tf`
- Modify: `terraform/environments/production/identity.tf`
- Modify: `terraform/environments/production/cloud_run.tf`
- Modify: `terraform/environments/production/outputs.tf`
- Modify: `terraform/environments/production/terraform.tfvars.example`
- Modify: `.github/workflows/deploy.yml`
- Test: `tests/unit/test_terraform.py`

### Step 1: Write failing infrastructure assertions

Assert source contains:

- Cloud Tasks API and regional queue with bounded retry/rate settings;
- `eva-action-dispatch-production` subscription filtered to
  `action.execution.requested`;
- dedicated task-caller service account;
- worker queue-enqueuer permission;
- task-caller `roles/run.invoker` on only `eva-action-executor`;
- private executor Cloud Run service without `allUsers` IAM;
- executor command `uvicorn eva_ai.actions.api:app --host 0.0.0.0 --port 8080`;
- OIDC audience/default URL environment;
- deploy workflow pauses worker, applies/migrates, then restores desired worker count;
- output for executor URL without making the service public.

### Step 2: Run tests and confirm failure

Run:

```bash
uv run pytest tests/unit/test_terraform.py -q
```

Expected: infrastructure resource assertions fail.

### Step 3: Implement Terraform and CI changes

Use Cloud Run service IAM, not application-shared-secret authentication. Give the executor service
account Cloud SQL client and Secret Manager access for existing connector grants; do not grant it
Pub/Sub subscriber or Terraform deployment permissions. Give the worker only Cloud Tasks enqueue
rights, not Cloud Run administration.

### Step 4: Format and validate

Run:

```bash
terraform fmt -recursive terraform
terraform -chdir=terraform/bootstrap init -backend=false
terraform -chdir=terraform/bootstrap validate
terraform -chdir=terraform/environments/production init -backend=false
terraform -chdir=terraform/environments/production validate
uv run pytest tests/unit/test_terraform.py -q
```

Expected: formatting, validation, and tests pass.

### Step 5: Commit

```bash
git add terraform .github/workflows/deploy.yml tests/unit/test_terraform.py
git commit -m "infra: deploy private Gmail action executor"
```

---

## Task 12: Document Reauthorization and Run the Full Verification Gate

**Files:**

- Modify: `.env.example`
- Modify: `README.md`
- Create: `docs/operations/gmail-actions-smoke-test.md`
- Modify: `docs/superpowers/specs/2026-09-17-milestone-8-actions-approval-design.md`
- Test: all affected tests

### Step 1: Write operator documentation

Document:

- declaring `gmail.compose` in Google Auth Platform;
- re-running `eva gmail connect` for the existing connector without resetting its watch/history;
- expected `REAUTHORIZATION_REQUIRED` behavior before reauthorization;
- action environment variables;
- approval/revision/expiry semantics;
- the exact controlled smoke sequence from design section 15.4;
- how to inspect Action/Approval/Result IDs without printing email content or secrets;
- how `UNKNOWN` is handled and why it must not be manually retried as a blind send.

Do not place real recipients, OAuth values, callback tokens, or test message bodies in the docs.

### Step 2: Run formatting and static checks

Run:

```bash
uv run ruff format src migrations tests
uv run ruff check .
uv run mypy src migrations tests
```

Expected: all pass with no modifications remaining from check mode.

### Step 3: Run unit and integration suites

Run:

```bash
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run alembic upgrade head
make verify
```

Expected: every command exits zero.

### Step 4: Run secret and placeholder scans

Run:

```bash
rg -n "TODO|FIXME|placeholder|NotImplementedError" src/eva_ai/actions \
  src/eva_ai/connectors/gmail/actions.py src/eva_ai/connectors/gmail/mime.py \
  docs/operations/gmail-actions-smoke-test.md
rg -n "ya29\\.|1//|Bearer |refresh_token|client_secret|AA[A-Za-z0-9_-]{20,}" \
  src tests terraform docs .github --glob '!docs/superpowers/plans/**'
```

Expected: the placeholder scan has no matches in new production paths; the secret scan has no real
credential material. Test fixtures must use unmistakably fake values.

### Step 5: Review the branch diff against the approved design

Run:

```bash
git diff --check origin/main...HEAD
git status --short
git diff --stat origin/main...HEAD
```

Manually verify every completion criterion in design section 16 is either covered by an automated
test or explicitly reserved for the controlled live smoke.

### Step 6: Commit documentation and final fixes

```bash
git add .env.example README.md docs/operations \
  docs/superpowers/specs/2026-09-17-milestone-8-actions-approval-design.md src migrations tests
git commit -m "docs: add Gmail action operations guide"
```

### Step 7: Push and open the milestone PR

```bash
git push -u origin codex/milestone-8-actions-approval
gh pr create \
  --base main \
  --head codex/milestone-8-actions-approval \
  --title "Milestone 8: safe Gmail actions and approvals" \
  --body-file docs/superpowers/plans/2026-09-17-milestone-8-actions-approval.md
```

Expected: GitHub returns the pull-request URL. Do not merge it; hand the URL and verification
evidence to the user for review.

---

## Review Checkpoints

1. **After Task 4:** Review the immutable action model, database constraints, Gmail scopes, and MIME
   output before wiring any provider execution.
2. **After Task 7:** Review provider-boundary crash semantics and the private executor before adding
   user-facing approval callbacks.
3. **After Task 9:** Run the end-to-end fake-provider flows for create/send/change/discard and verify
   old approvals cannot authorize revised content.
4. **After Task 11:** Review Terraform/IAM least privilege and the deployment plan before any apply.
5. **After Task 12:** Request code review, address findings, rerun the complete verification gate,
   then push and open the PR.
