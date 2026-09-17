# Gmail actions: authorization, operations, and controlled smoke test

Milestone 8 lets Eva create and revise Gmail drafts, then send or discard them only after an exact
Telegram decision. The model never receives a Gmail write tool. Deterministic services validate the
intent, persist immutable action state, and execute through an IAM-authenticated private Cloud Run
service.

Use only an operator-controlled mailbox and recipient for this smoke test. Do not paste OAuth
material, callback tokens, email addresses, subjects, bodies, or provider responses into terminals,
logs, issues, documentation, or commits.

## Authorization checkpoint

In Google Auth Platform for Eva's project:

1. Keep the OAuth app **In production** and the configured application domain verified.
2. Under **Data access**, declare only these Gmail scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
3. Complete any Google verification required for the restricted `gmail.compose` scope before
   onboarding users outside the approved test audience.

Declaring the scope does not change an existing refresh grant. Reauthorize the existing connector
from a trusted operator machine, using the User and Workspace that already own it:

```bash
export EVA_USER_ID=EXISTING_USER_UUID
export EVA_WORKSPACE_ID=EXISTING_WORKSPACE_UUID
uv run eva gmail connect --user-id "$EVA_USER_ID" --workspace-id "$EVA_WORKSPACE_ID"
```

Select the connector's existing Gmail identity and consent to both scopes. Re-running `connect`
creates a new Secret Manager version and validates the Gmail profile, but preserves the connector
row, original `connected_at` boundary, history cursor, and watch state. Do not create a replacement
User, Workspace, or connector to add the scope.

Before reauthorization, ingestion credentials that still have `gmail.readonly` can read mail, but
write attempts fail closed. Eva records the action failure as `action_reauthorization_required`,
marks the connector `REAUTHORIZATION_REQUIRED`, performs no Gmail mutation, and asks for a fresh
grant. Re-running the command above restores `ACTIVE` after the new grant and watch are validated.

Confirm scope and connector state without selecting credential material:

```sql
SELECT id, status, granted_scopes, connected_at
FROM connector_accounts
WHERE id = :'connector_id';

SELECT connector_account_id, history_id, watch_expiration,
       next_watch_renewal_at, next_safety_sync_at
FROM gmail_sync_states
WHERE connector_account_id = :'connector_id';
```

## Runtime configuration

Actions are disabled by default in the proposal and dispatch runtimes. When
`EVA_ACTIONS_ENABLED=true`, Telegram processing/delivery must also be enabled and every public API
or worker runtime must receive a complete action configuration. The private Cloud Run service uses
`EVA_ACTION_EXECUTOR_ENABLED=true` independently:

| Variable | Purpose |
| --- | --- |
| `EVA_ACTION_DISPATCH_SUBSCRIPTION_ID` | Filtered Pub/Sub subscription for execution requests |
| `EVA_ACTION_TASKS_PROJECT_ID` | Project that owns the queue |
| `EVA_ACTION_TASKS_LOCATION` | Queue and executor region |
| `EVA_ACTION_TASKS_QUEUE_ID` | Bounded Cloud Tasks queue |
| `EVA_ACTION_EXECUTOR_URL` | Private executor route ending in `/internal/actions/execute` |
| `EVA_ACTION_EXECUTOR_AUDIENCE` | Executor's default Cloud Run URL used as OIDC audience |
| `EVA_ACTION_TASK_CALLER_SERVICE_ACCOUNT` | Identity attached to task OIDC tokens |
| `EVA_ACTION_APPROVAL_TTL_HOURS` | Exact approval lifetime; fixed at 24 hours |
| `EVA_ACTION_REVISION_TTL_SECONDS` | Time allowed for the next Change instruction |
| `EVA_ACTION_LEASE_SECONDS` | Durable execution claim lease |
| `EVA_ACTION_TASK_TIMEOUT_SECONDS` | Per-task execution deadline |
| `EVA_ACTION_TASK_MAX_ATTEMPTS` | Bounded delivery attempts |
| `EVA_ACTION_TASK_RETRY_INITIAL_BACKOFF_SECONDS` | Initial application retry bound |
| `EVA_ACTION_TASK_RETRY_MAX_BACKOFF_SECONDS` | Maximum application retry bound |

Terraform supplies these values in production. Before the first Milestone 8 deployment, reapply the
bootstrap stack so the deployer can enable Cloud Tasks and manage the queue. Keep actions off until
the connector grant is ready:

```bash
terraform -chdir=terraform/bootstrap init
terraform -chdir=terraform/bootstrap plan -out=bootstrap.tfplan
terraform -chdir=terraform/bootstrap apply bootstrap.tfplan

gh variable set EVA_ACTIONS_ENABLED --body true
gh workflow run "Deploy Eva to GCP"
```

The production workflow pauses the shared worker, applies infrastructure, migrates the database,
restores the desired worker count, and verifies that `eva-action-executor` uses internal ingress
with no `allUsers` IAM grant. The private executor itself remains bootable while proposal/dispatch
is disabled, allowing deployment health checks and completion of already-authorized durable tasks;
it is still callable only by the task-caller identity.

## Approval and revision semantics

- Draft creation may run automatically after an agent or Telegram request passes deterministic
  validation. The Gmail draft is visible before sending.
- **Send** grants one immutable proposal version. Recipients, subject, complete body, thread
  context, and attachment-free state must match its stored canonical hash at execution time.
- **Change** immediately supersedes the current approval. The next paired-user message supplies a
  bounded revision instruction; Eva writes a complete replacement draft and issues a fresh card.
- **Discard** rejects the send approval and deletes only the exact Eva-managed Gmail draft.
- Approval buttons work only for the paired numeric Telegram user and private chat. Tokens are
  random, short-lived, stored only as SHA-256 digests, and invalid after use, expiry, discard, or
  revision.
- Approval expires after 24 hours. Revision mode expires separately after the configured short
  window. Expired state performs no Gmail mutation.
- Duplicate Pub/Sub deliveries, tasks, and callbacks are expected. Durable terminal state makes
  them idempotent.

## Controlled live smoke

Run these checkpoints in order and record only Eva resource IDs and status values:

1. Confirm the connector is `ACTIVE` with both declared scopes and actions are enabled.
2. In the paired private Telegram chat, ask Eva to create a brand-new email draft addressed only to
   an operator-controlled recipient. Choose the recipient and message content at run time.
3. Confirm the exact draft exists in Gmail and the Telegram card matches it completely.
4. Choose **Change**, provide a revision, and confirm Gmail contains the replacement draft and a new
   approval card. Press an old button and confirm it is rejected as stale.
5. Choose **Discard** on the new card. Confirm only that managed draft is deleted and no message was
   sent.
6. Select an existing operator-controlled test thread and ask Eva to prepare a reply. Confirm the
   draft remains in that thread.
7. Review every displayed field, choose **Send**, and confirm exactly one sent message plus one
   concise Telegram completion.
8. Press the same Telegram button again. Confirm it reports an already-decided/stale result and no
   second Gmail message appears.
9. Replay the completed Action through Cloud Tasks using the procedure below. Confirm the terminal
   result is reused and no second Gmail message appears.
10. Inspect the action, approval, result, managed-draft, outbox, and notification status rows using
    only the safe queries below. Check application logs for IDs and sanitized outcome categories;
    there must be no email content, full address list, provider payload, raw callback, or secret.

## Safe audit inspection

Run `psql` against the intended database through the existing trusted local connection or Cloud SQL
Auth Proxy. Bind `workspace_id` and `action_id` as psql variables; do not print the database URL.

```sql
SELECT p.id AS proposal_id, p.capability, p.status AS proposal_status, p.version,
       p.created_at, p.expires_at,
       a.id AS approval_id, a.status AS approval_status, a.requested_at,
       a.decided_at, a.expires_at AS approval_expires_at
FROM action_proposals p
LEFT JOIN action_approvals a ON a.proposal_id = p.id
WHERE p.workspace_id = :'workspace_id'
ORDER BY p.created_at DESC
LIMIT 20;

SELECT x.id AS action_id, x.proposal_id, x.capability, x.status,
       x.attempt_count, x.failure_code, x.created_at, x.started_at, x.finished_at,
       r.outcome, r.provider_status_category
FROM actions x
LEFT JOIN action_results r ON r.action_id = x.id
WHERE x.workspace_id = :'workspace_id'
ORDER BY x.created_at DESC
LIMIT 20;

SELECT id, status, send_proposal_version, created_at, updated_at, sent_at, deleted_at
FROM managed_gmail_drafts
WHERE workspace_id = :'workspace_id'
ORDER BY updated_at DESC
LIMIT 20;

SELECT id, message_type, destination, state, attempt_count, published_at
FROM outbox_messages
WHERE message_type = 'action.execution.requested'
ORDER BY created_at DESC
LIMIT 20;
```

These queries intentionally omit proposal parameters, hashes, callback digests, provider message
content, notification text, and credential references.

### Controlled Cloud Task replay

Only replay an Action already shown as terminal (`SUCCEEDED`, `FAILED`, or `UNKNOWN`). Never use
this procedure for a queued/running action or as a way to recover an uncertain send.

```bash
export PROJECT_ID=YOUR_PROJECT_ID
export REGION=YOUR_REGION
export ACTION_ID=TERMINAL_ACTION_UUID
export EXECUTOR_AUDIENCE="$(terraform -chdir=terraform/environments/production output -raw action_executor_url)"
export EXECUTOR_URL="${EXECUTOR_AUDIENCE}/internal/actions/execute"
export TASK_CALLER="eva-action-task-caller@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud tasks create-http-task \
  --project="$PROJECT_ID" \
  --location="$REGION" \
  --queue=eva-actions-production \
  --url="$EXECUTOR_URL" \
  --method=POST \
  --header=Content-Type:application/json \
  --body-content="{\"action_id\":\"${ACTION_ID}\",\"schema_version\":1}" \
  --oidc-service-account-email="$TASK_CALLER" \
  --oidc-token-audience="$EXECUTOR_AUDIENCE"
```

The operator needs permission to create a task and act as the task-caller service account. The
private executor must return the existing terminal outcome without another Gmail provider call.
Unset the local IDs afterward.

## Failure handling

- `action_reauthorization_required`: re-run `eva gmail connect` for the existing User/Workspace and
  Gmail identity. Do not reset its watch or history cursor.
- `FAILED`: inspect only `failure_code`, sanitized summary, attempt count, and correlated IDs. Fix
  the pre-provider cause before creating a new proposal.
- `UNKNOWN`: the provider call may have succeeded even though Eva could not persist a definitive
  response. Do not reset the row, manually replay the task, or approve a replacement send. Check
  Gmail's Sent/Drafts state through the operator mailbox and preserve the audit trail for explicit
  reconciliation. Milestone 9 will add broader automated repair.
- Missing Telegram completion does not imply Gmail failure. Inspect Action/Result state first;
  notification delivery is deliberately independent from a completed Gmail mutation.
- A failed deployment before migrations leaves the shared worker at zero. Fix and rerun the
  workflow; durable Pub/Sub messages remain available.
