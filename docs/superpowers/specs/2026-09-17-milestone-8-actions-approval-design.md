# Milestone 8 Actions and Approval Design

**Date:** 2026-09-17
**Status:** Approved in conversation; pending written-spec review

## 1. Objective

Milestone 8 gives Eva her first privileged external actions while preserving the system's central
trust boundary: the model may propose an action, but deterministic application code alone may
authorize and execute it.

Eva will be able to:

- create Gmail drafts automatically from proactive AgentRuns and explicit Telegram requests;
- reply in an existing Gmail thread or compose a brand-new email;
- resolve a named recipient from bounded prior Gmail correspondence and ask when resolution is
  ambiguous;
- present the exact draft in Telegram with **Send**, **Change**, and **Discard** controls;
- revise a draft from a natural-language Telegram instruction;
- require an exact, immutable, 24-hour approval before sending;
- delete only the exact Eva-managed draft when the authenticated user chooses **Discard**; and
- execute through Cloud Tasks with idempotent state transitions and a complete audit trail.

This milestone intentionally does not add image, video, document, voice, or attachment
understanding. That work follows as Milestone 8.1 so privileged-action safety and untrusted-file
processing remain separate security boundaries.

## 2. Design Principles

1. **The model proposes; application services decide and execute.** No LLM receives a direct Gmail
   write tool.
2. **External content is evidence, never authority.** Email bodies, headers, tool results, and
   recipient text cannot change identity, policy, scope, or approval.
3. **Approval is exact.** A grant is bound to one immutable proposal version and the canonical hash
   of its recipients, subject, body, thread context, and attachment-free state.
4. **Provider calls are outside database transactions.** Durable state is claimed before a call and
   finalized afterward.
5. **No blind duplicate sends.** An uncertain send outcome becomes `UNKNOWN` and is not retried
   automatically.
6. **Every important transition is durable and explainable.** Proposal, policy, approval, dispatch,
   execution, and outcome are persisted with provenance.
7. **Tenant scope is supplied by trusted application state.** The database remains the final
   User/Workspace/connector isolation boundary.
8. **Least privilege is explicit.** Gmail authorization expands only to `gmail.readonly` and
   `gmail.compose`; the action capability allowlist exposes no label, archive, received-message
   deletion, or unrestricted mailbox mutation.

## 3. Scope

### 3.1 Included

- Immutable ActionProposal lifecycle
- Deterministic PolicyEngine
- Exact Approval lifecycle with a 24-hour default expiry
- Action and ActionResult persistence
- Managed Gmail draft projection
- Gmail draft create, update, delete, and send adapters
- Existing-thread replies and brand-new email composition
- Bounded recipient resolution from Gmail history
- Proactive automatic draft creation
- Reactive draft creation from Telegram conversation
- Telegram Send, Change, and Discard callbacks
- Natural-language revision state
- Transactional outbox events and Pub/Sub action dispatch
- Cloud Tasks queue and authenticated private executor
- Terraform and GitHub Actions deployment changes
- OAuth reauthorization for `gmail.compose`
- Unit, integration, infrastructure, and controlled live-smoke coverage

### 3.2 Excluded

- Reading or attaching image, video, audio, PDF, or document contents
- Adding, forwarding, resending, or inspecting attachments
- Gmail labels, archive, trash, or received/sent-message deletion
- Fully autonomous email sending
- Time-window or blanket approval such as "send any email for five minutes"
- Calendar or other connector actions
- Multi-step workflow orchestration beyond one managed draft lifecycle
- Automatic retry of a send with an uncertain provider outcome
- General reliability reconciliation and stuck-action repair beyond the bounded safeguards required
  for safe Milestone 8 execution; broader hardening remains Milestone 9

## 4. Architecture

### 4.1 Trust boundary

The OpenAI investigation and conversation adapters continue to expose read-only Gmail tools. Their
structured output may include an action intent, but it cannot contact Gmail's write API.

Representative intent:

```json
{
  "capability": "gmail.create_draft",
  "arguments": {
    "mode": "reply",
    "thread_id": "provider-thread-id",
    "to": ["person@example.com"],
    "cc": [],
    "bcc": [],
    "subject": "Re: Interview availability",
    "text_body": "...",
    "html_body": "..."
  },
  "description": "Prepare a reply for the recruiter",
  "requires_approval": false
}
```

The action application service replaces any model-supplied ownership or provenance with the
authenticated User, Workspace, connector, Situation, AgentRun, or conversation turn. It then
normalizes and validates the arguments before persisting a proposal.

### 4.2 Components

```text
AgentRun or Telegram turn
        -> ActionProposalService
        -> deterministic validation and canonical hashing
        -> PolicyEngine
        -> ActionService + transactional Outbox
        -> existing Outbox relay -> eva-events Pub/Sub
        -> action-dispatch subscription
        -> Cloud Tasks queue
        -> private eva-action-executor Cloud Run service
        -> scoped Gmail action adapter
        -> ActionResult + Outbox
        -> Telegram notification/delivery
```

The private executor is a separate Cloud Run service using the shared application image and a
dedicated FastAPI entry point. It does not expose Telegram or public application routes. Cloud Run
IAM permits invocation only by a dedicated Cloud Tasks caller service account using an OIDC ID
token.

### 4.3 Capability policy

The initial closed capability set is:

| Capability | Default policy | Additional requirement |
|---|---|---|
| `gmail.create_draft` | `ALLOW` | Valid scoped connector and normalized message |
| `gmail.update_draft` | `ALLOW` | Authenticated revision state for the exact managed draft |
| `gmail.send_draft` | `REQUIRE_APPROVAL` | Current exact approval and unchanged hash |
| `gmail.delete_draft` | `ALLOW` | Explicit authenticated Discard callback for exact draft |

All unknown capabilities return `DENY`. Attachments, labels, archive, trash, generic message
deletion, and direct `gmail.messages.send` are not registered capabilities.

## 5. Persistence Model

### 5.1 `action_proposals`

Representative fields:

- `id`, `user_id`, `workspace_id`
- `connector_account_id`
- optional `situation_id`, `goal_id`, `agent_run_id`, `conversation_turn_id`
- `origin` (`AGENT_RUN`, `TELEGRAM_USER`, `SYSTEM`)
- `capability`
- canonical `parameters_json`
- `parameters_hash`
- `description`
- `risk_level`
- `policy_decision`
- `status`
- `version`
- optional `supersedes_proposal_id`
- `created_at`, `expires_at`, and terminal timestamps

Representative statuses:

```text
PROPOSED
QUEUED
WAITING_APPROVAL
APPROVED
REJECTED
EXPIRED
SUPERSEDED
COMPLETED
FAILED
CANCELLED
```

Parameters are immutable after insert. A material change creates a new version linked with
`supersedes_proposal_id`.

Each proposal represents exactly one capability. Draft creation and sending are therefore two
different proposals:

- an allowed `gmail.create_draft` proposal completes when Gmail creates the managed draft; then
- Eva creates a separate `gmail.send_draft` proposal containing the exact current message and puts
  that proposal into `WAITING_APPROVAL`.

The creation proposal can never change capability or become authority to send.

### 5.2 `approvals`

Representative fields:

- `id`, `proposal_id`, `user_id`, `workspace_id`
- `parameters_hash`
- `principal_type` and persisted Telegram account/chat identifiers
- `status` (`PENDING`, `GRANTED`, `REJECTED`, `EXPIRED`, `SUPERSEDED`)
- opaque callback token digest
- `requested_at`, `decided_at`, `expires_at`

The raw callback token is never stored or logged. A unique constraint permits only one effective
grant for a proposal version. Replayed callbacks return the existing decision.

### 5.3 `actions`

Representative fields:

- `id`, `proposal_id`, `user_id`, `workspace_id`
- `capability`
- deterministic `idempotency_key`
- optional Cloud Task name
- `status` (`QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `UNKNOWN`)
- execution lease, attempt metadata, and `provider_call_started_at`
- `created_at`, `started_at`, `finished_at`

The unique idempotency key prevents more than one logical action for the same proposal and
capability transition.

### 5.4 `action_results`

Representative fields:

- `action_id`
- outcome category
- sanitized provider status category
- provider draft/message/thread identifiers where safe
- result metadata required for idempotency and support
- `created_at`

Raw provider responses, OAuth values, email bodies, and full recipient lists are not duplicated into
result rows or logs.

### 5.5 `managed_gmail_drafts`

Representative fields:

- `id`, `user_id`, `workspace_id`, `connector_account_id`
- provider draft ID and current provider message ID
- optional Gmail thread ID
- originating create proposal ID
- active send proposal ID and send-proposal version
- current canonical content hash
- deterministic RFC message identifier used for bounded outcome correlation
- state (`CREATING`, `READY`, `UPDATING`, `SENDING`, `SENT`, `DELETING`, `DELETED`, `UNKNOWN`)
- timestamps

This projection lets Eva replace the content of a stable Gmail draft while retaining immutable
proposal history. Composite foreign keys prevent cross-scope relationships.

## 6. Canonical Message Contract

The application normalizes message parameters before hashing:

- mode: `NEW` or `REPLY`
- normalized `to`, `cc`, and `bcc` address lists
- subject
- UTF-8 text body
- optional sanitized HTML alternative generated from the same content
- optional Gmail thread ID
- reply headers derived from the scoped Gmail thread, not supplied by model text
- attachments fixed to an empty list in Milestone 8

The canonical JSON encoding uses stable key order and normalized strings. The SHA-256 parameter
hash covers all fields that affect what Gmail sends. Telegram approval displays the human-readable
values, while the persisted approval binds to this hash.

For replies, the Gmail adapter supplies the target thread ID and RFC-compliant `In-Reply-To` and
`References` headers and preserves a matching subject so Gmail retains thread context. For a new
email, no thread identifier is supplied.

## 7. Recipient Resolution

An explicit email address is validated directly. When the user names a person without an address,
Eva performs a bounded, scoped search over prior Gmail correspondence and extracts candidate
addresses from trusted tool output.

- Exactly one suitable match permits draft creation.
- Multiple plausible matches produce a Telegram clarification and no draft.
- No match asks the user for an address.
- The final approval card always displays the exact `To`, `Cc`, and `Bcc` addresses.
- Recipient lookup never searches another connector or Workspace.

Google Contacts is not added in this milestone.

## 8. End-to-End Flows

### 8.1 Automatic draft creation

1. A proactive AgentRun or reactive Telegram turn returns a structured draft intent.
2. The application resolves scope and recipient identity and validates the normalized message.
3. One transaction inserts an immutable `gmail.create_draft` proposal, evaluates the deterministic
   policy, creates a queued action, and writes `action.execution.requested` to the Outbox.
4. The existing relay publishes the event to Pub/Sub.
5. The action dispatcher creates a deterministically named Cloud Task and acknowledges Pub/Sub only
   after a successful enqueue or a matching `AlreadyExists` result.
6. The private executor claims the action, calls `users.drafts.create`, persists the managed draft
   and result, and completes the create proposal.
7. In the same result transaction, Eva creates a separate immutable `gmail.send_draft` proposal for
   the exact persisted message, puts it into `WAITING_APPROVAL`, and writes the next Outbox events.
8. Telegram receives a draft card with **Send**, **Change**, and **Discard** for that send proposal.

### 8.2 Send approval

1. **Send** callback data resolves an opaque Approval identifier.
2. Eva verifies the persisted paired Telegram user and private chat.
3. One transaction locks and reloads the proposal, approval, managed draft, connector, and scope.
4. Eva rejects expired, superseded, already terminal, scope-mismatched, or hash-mismatched requests.
5. Eva records the exact grant and queues `gmail.send_draft` with an Outbox event.
6. The executor revalidates policy, expiry, active version, connector authorization, Gmail draft
   identity, and content hash immediately before `users.drafts.send`.
7. Success records the sent Gmail message/thread IDs, completes the proposal, and notifies Telegram.

Repeated callbacks and queue deliveries return the existing action or result and never create a
second logical send.

### 8.3 Revision

1. **Change** immediately marks a pending approval `SUPERSEDED` and records bounded conversation
   state pointing to the managed draft and active proposal.
2. Eva asks for a natural-language revision instruction.
3. The next authenticated Telegram text is processed with the current exact message and instruction.
4. The model returns a complete replacement message, not a patch to execute directly.
5. Application code validates and canonicalizes the full replacement.
6. An allowed immutable `gmail.update_draft` proposal is inserted, while the old send proposal
   becomes `SUPERSEDED`.
7. `gmail.update_draft` replaces the content of the existing provider draft and completes the
   update proposal.
8. After the update succeeds, Eva creates a new immutable `gmail.send_draft` proposal version for
   the replacement content.
9. A fresh approval card is issued for the new send proposal.

No approval for an older version can authorize the revised message.

### 8.4 Discard

1. **Discard** resolves the exact managed draft and verifies paired Telegram ownership.
2. Pending approvals are invalidated and the proposal becomes `CANCELLED` before dispatch.
3. `gmail.delete_draft` is queued for only the persisted provider draft ID.
4. Success marks the managed draft `DELETED` and reports completion in Telegram.

The capability cannot delete received mail, sent mail, or an unrelated Gmail draft.

## 9. Dispatch and Execution Semantics

### 9.1 Transactional dispatch

No database transaction includes a Pub/Sub, Cloud Tasks, Gmail, Telegram, or model call. Action
state and an Outbox row commit atomically. The existing relay publishes the durable event. A new
action-dispatch subscription is responsible only for translating a validated execution request into
a Cloud Task.

Cloud Task names derive from a hash of the Action ID and capability so enqueue redelivery is
idempotent. Task bodies contain only an opaque Action ID and schema version.

### 9.2 Private executor authentication

Terraform creates:

- a regional Cloud Tasks queue;
- a task-caller service account;
- queue enqueue permissions for the action dispatcher runtime;
- token-creation/act-as permissions only where required;
- Cloud Run invoker permission for the task-caller on `eva-action-executor`; and
- a private Cloud Run service with no unauthenticated invocation.

Cloud Tasks attaches an OIDC ID token whose audience is the executor's default Cloud Run URL.
Cloud Run IAM rejects unauthorized invocation before application code runs.

### 9.3 Claims and retries

The executor transactionally claims a queued Action with a bounded lease. A concurrent or replayed
request observes the current state and returns the existing terminal result or a retry-safe response.

Immediately before a non-idempotent Gmail mutation, the executor commits
`provider_call_started_at`, then performs the provider call outside the transaction. If the process
dies after that boundary and a later delivery finds an expired lease without a persisted result, it
does not repeat a send. It marks the action `UNKNOWN` for later reconciliation. This deliberately
prefers a missed action requiring review over a duplicate email.

- Failures before a provider call may retry with bounded backoff.
- Known retryable provider rejections may retry only where repeating the exact operation is safe.
- A revoked OAuth grant marks the connector `REAUTHORIZATION_REQUIRED` for actions.
- Any ambiguous send result is persisted as `UNKNOWN`, reported to the user, and acknowledged to
  Cloud Tasks so it is not sent again automatically.

Milestone 9 adds broader reconciliation and repair for unknown or stuck actions.

## 10. Telegram Interaction

Draft cards show:

- whether the message is a reply or new email;
- exact To/Cc/Bcc values;
- subject;
- complete body, split safely across Telegram messages when necessary;
- approval expiry; and
- **Send**, **Change**, and **Discard** controls.

Callback payloads contain no email data or direct database IDs. They use short random tokens whose
digests map to the persisted Approval/draft intent. Callback queries are answered promptly, while
durable processing continues through the existing conversation and delivery paths.

The user receives one concise status update for queued, completed, failed, expired, discarded, or
unknown outcomes. Telegram delivery failure does not roll back a completed Gmail action.

## 11. OAuth and Gmail Integration

The connector authorization set becomes:

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.compose
```

`gmail.compose` permits Gmail draft management and sending without granting the permanent deletion
power of the full-mail scope. It is a restricted scope and must be declared in Google Auth Platform;
public use requires the applicable Google verification and data-handling posture.

An existing connector that lacks `gmail.compose` may continue read ingestion but is unavailable for
write actions. It is marked as needing action reauthorization rather than destroying its Gmail watch
or history cursor. Re-running the scoped connection flow obtains explicit consent, writes a new
Secret Manager version, validates the authorized Gmail identity, and preserves connector ownership
and ingestion state.

OAuth codes, access tokens, refresh tokens, and client credentials remain excluded from normal
settings, PostgreSQL, logs, Terraform state, Pub/Sub messages, and Cloud Tasks payloads.

## 12. Error Handling

### 12.1 Permanent validation errors

The following fail without a provider call:

- invalid or missing recipients;
- ambiguous recipient resolution;
- missing or cross-scope connector/thread/draft references;
- unsupported capability or non-empty attachment list;
- stale or superseded proposal;
- expired approval;
- parameter or Gmail draft content hash mismatch; and
- callback from an unpaired Telegram principal or different private chat.

### 12.2 Provider and infrastructure errors

- OAuth revocation or invalid grant marks the connector for reauthorization and stops hot retries.
- Quota, timeout, and server failures are classified before retry decisions.
- Draft creation/update/delete failures retain durable state and surface a sanitized Telegram
  explanation.
- An uncertain send becomes `UNKNOWN` and is never blindly retried.
- Pub/Sub redelivery, duplicate Cloud Tasks, and duplicate Telegram callbacks are expected and
  idempotent.

Logs contain resource IDs, state transitions, attempt counts, durations, and sanitized categories.
They exclude email subjects, bodies, full addresses, raw callbacks, provider responses, and secrets.

## 13. Code Organization

Expected additions follow the existing modular-monolith pattern:

```text
src/eva_ai/actions/
    contracts.py
    errors.py
    policy.py
    repository.py
    service.py
    types.py
    dispatcher.py
    executor.py
    api.py

src/eva_ai/connectors/gmail/
    actions.py
    mime.py
```

Existing agent and conversation application services gain a narrow dependency on the proposal
application boundary. Telegram callback handling delegates to Approval and revision services rather
than containing policy or Gmail logic.

Comments will document non-obvious authorization, hashing, provider-idempotency, retry, transaction,
and tenant-scoping invariants.

## 14. Configuration and Deployment

New configuration is equivalent to:

- Cloud Tasks project, region, and queue ID
- action executor URL and OIDC audience
- task-caller service account
- action-dispatch Pub/Sub subscription
- proposal and approval expiry defaults
- executor lease, timeout, and bounded retry settings

Terraform adds the queue, subscription, private Cloud Run executor, service account, IAM bindings,
and runtime configuration. GitHub Actions continues to validate Terraform on pull requests and apply
the production configuration after merge to `main` under the existing deployment controls.

The existing worker pool gains the action-dispatch consumer but remains one shared worker process.
The private executor is request-driven and scales to zero when idle.

## 15. Testing Strategy

### 15.1 Unit tests

- Closed capability registry and PolicyEngine decisions
- Canonical normalization and deterministic parameter hashing
- Proposal, Approval, Action, and managed-draft transition rules
- 24-hour approval expiry
- MIME construction for new messages and replies
- recipient validation and bounded resolution outcomes
- retry and unknown-outcome classification
- callback token hashing and stale/superseded behavior

### 15.2 Integration tests

- Database ownership and cross-scope constraints
- proposal plus Outbox atomicity
- duplicate proposal, Pub/Sub, Cloud Task, and callback delivery
- fake Gmail draft create/update/delete/send
- proactive and reactive draft creation
- exact Send approval and single execution
- Change invalidating the old approval and issuing a new version
- Discard deleting only the managed draft
- old approvals rejected after revision
- expired approval rejected without Gmail invocation
- revoked connector behavior
- crash before and after provider boundaries
- sanitized persistence and logging

### 15.3 Infrastructure and CI tests

- Terraform formatting and validation
- queue, service account, IAM, subscription, and executor plan assertions
- private executor invocation configuration
- application verification suite and migration upgrade test

### 15.4 Controlled live smoke

After explicit operator checkpoints:

1. Add `gmail.compose` to Google Auth Platform and reauthorize the configured personal connector.
2. Create a new email draft to an operator-controlled address.
3. Revise it and confirm the old approval is unusable.
4. Discard it and confirm only that Gmail draft is deleted.
5. Create a reply draft in an existing test thread.
6. Approve and send it.
7. Replay the Telegram callback and Cloud Task request and confirm exactly one sent message.
8. Confirm action events, audit rows, and Telegram completion without sensitive logs.

Live email recipients and contents are selected by the operator and are never embedded in tests,
documentation, commits, or CI.

## 16. Completion Criteria

Milestone 8 is complete when:

- Eva can proactively or reactively create a Gmail reply or brand-new draft;
- ambiguous named recipients cause clarification instead of a guessed draft;
- the Gmail draft is visible before approval;
- Send approval is exact, authenticated, immutable, and expires after 24 hours;
- Change produces a new version and invalidates every older approval;
- Discard deletes only the exact managed Gmail draft;
- send executes through authenticated Cloud Tasks and succeeds at most once logically;
- uncertain send outcomes are not automatically retried;
- all state transitions survive restarts and are auditable;
- OAuth and logs preserve the documented privacy boundaries;
- Terraform and CI deploy the new resources; and
- automated tests and the controlled live smoke pass.

## 17. References

- [Choose Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [Create and send Gmail drafts](https://developers.google.com/workspace/gmail/api/guides/drafts)
- [Gmail thread behavior](https://developers.google.com/workspace/gmail/api/guides/threads)
- [Gmail API REST reference](https://developers.google.com/workspace/gmail/api/reference/rest)
- [Create authenticated HTTP target tasks](https://docs.cloud.google.com/tasks/docs/creating-http-target-tasks)
- [Create Cloud Tasks tasks](https://docs.cloud.google.com/tasks/docs/create-tasks)
