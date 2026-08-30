# Milestone 2 Gmail Ingestion Design

**Status:** Approved for implementation

**Date:** 2026-08-30

**Product specification:** `spec/2026-08-29-proactive-personal-ai-agent-design.md`

## Objective

Connect Eva to one personal Gmail mailbox and turn every newly received inbox message into a durable canonical `email.received` Event. The integration uses Google OAuth 2.0, Gmail mailbox watches, and a real Google Cloud Pub/Sub pull subscription while preserving the Event + Outbox reliability boundary built in Milestone 1.

This milestone proves the live path from a new message in `saswatray2505@gmail.com` through provider notification, Gmail history synchronization, message normalization, idempotent Event persistence, and Pub/Sub acknowledgement. It does not ingest messages that predate the initial connection.

## Decisions

- Work is developed on `codex/milestone-2-gmail-ingestion`, pushed to GitHub, and delivered through a pull request to `main`.
- The Google Cloud project is `evaai-507018`.
- Gmail uses its Pub/Sub watch integration; periodic mailbox polling is not the primary ingestion mechanism.
- Local development receives notifications through a real Pub/Sub pull subscription. A Cloud Run push endpoint is deferred until deployment work requires it.
- The initial connector is single-user but remains scoped by the existing User and Workspace ownership model.
- Only messages arriving after the connector is established are ingested. Initial historical backfill is excluded.
- All new messages carrying the Gmail `INBOX` label are included, including Primary, Promotions, Social, and Updates. Gmail label IDs are preserved for later relevance filtering.
- Normalized Events contain message headers and readable bodies. Attachment metadata is retained, but attachment binaries are not downloaded.
- Milestone 2 requests only the `gmail.readonly` OAuth scope. It cannot send, modify, label, archive, or delete email.
- OAuth credentials are stored in Google Secret Manager. PostgreSQL stores only the secret resource reference and non-secret connector state.
- Provider notifications and history are at least once. Duplicate delivery is absorbed by Event idempotency.
- External email content is untrusted data. It cannot define identity, authorization, scopes, policy, approval, or instructions for Eva.
- Code comments explain OAuth, cursor, acknowledgement, idempotency, MIME, and recovery invariants where the reasoning is not self-evident.

## Scope

Milestone 2 includes:

- Google Cloud Gmail, Pub/Sub, and Secret Manager API setup
- OAuth consent and desktop-client bootstrap instructions
- offline Google authorization for the personal mailbox
- ConnectorAccount persistence and ownership constraints
- GmailSyncState persistence and per-connector synchronization leases
- Secret Manager credential storage and retrieval boundary
- Gmail `users.watch` creation and renewal
- Pub/Sub pull notification consumption
- Gmail `users.history.list` pagination
- Gmail message retrieval and MIME normalization
- canonical `email.received` creation through the Milestone 1 EventService
- duplicate notification and message-history handling
- bounded recovery from an expired Gmail history cursor
- periodic watch and notification-silence repair checks
- redacted structured logging
- migrations, unit tests, PostgreSQL integration tests, adapter contract tests, and a documented live smoke test

Milestone 2 excludes:

- importing email received before the connector was established
- downloading or interpreting attachment contents
- Gmail search or thread-investigation tools
- drafts, sends, labels, archives, deletes, or any other Gmail mutation
- relevance classification, Signals, Goals, Situations, Memory, or Agents
- Cloud Run deployment or a public Pub/Sub webhook
- OAuth verification for a public multi-user application
- a general connector framework for providers other than Gmail
- user-facing connector administration UI

## Architecture

The connector is split into provider-facing adapters and application services:

```text
connect-gmail command
    -> Google OAuth consent
    -> verified Gmail profile cursor + local lower-bound timestamp
    -> Secret Manager credential version
    -> ConnectorAccount + GmailSyncState
    -> Gmail users.watch

Gmail mailbox change
    -> eva-gmail-notifications topic
    -> eva-gmail-ingestion-local pull subscription
    -> notification decoder
    -> Gmail history synchronizer
    -> Gmail message fetcher
    -> Gmail event normalizer
    -> EventService.ingest(NewEvent)
    -> Event + EventProcessing + Outbox
    -> cursor advance
    -> Pub/Sub acknowledge
```

The Gmail connector implements external-to-system behavior only: authorize an account, establish a watch, handle a notification, fetch changes, and normalize facts. Future Gmail tools that search, draft, or send are separate system-to-provider capabilities and do not share an unrestricted interface with ingestion.

## Google Cloud Resources

Milestone 2 enables the following services in `evaai-507018`:

- `gmail.googleapis.com`
- `pubsub.googleapis.com`
- `secretmanager.googleapis.com`

Milestone 2 creates:

- Pub/Sub topic `eva-gmail-notifications`
- pull subscription `eva-gmail-ingestion-local`
- one Secret Manager secret for the connected account's authorized-user credentials

The Gmail push service identity `gmail-api-push@system.gserviceaccount.com` receives `roles/pubsub.publisher` on the topic and no broader project role. The topic project ID must match the Google developer project used to call `users.watch`.

Setup and teardown commands are documented. Teardown is never run as part of tests or normal milestone completion.

## OAuth and Secret Handling

The Google Auth Platform configuration uses:

- audience: External
- publishing status: In production
- client type: Desktop app
- redirect: temporary localhost loopback callback opened by `connect-gmail`
- scope: `https://www.googleapis.com/auth/gmail.readonly`
- access type: offline

The app is personal-use and unverified. The user may pass Google's unverified-app warning during consent. Publishing the app avoids the seven-day refresh-token lifetime applied to an External app left in Testing. Public distribution or more than the personal-use user cap would require a separate verification decision.

The Milestone 2 local smoke used Testing mode only as an explicit local exception after production publishing was blocked on public branding, privacy, and terms URLs. That local token may expire after seven days and require reconnection. The exception is not production-readiness evidence: External + In production, the public URLs, and credential rotation remain deployment gates.

The downloaded OAuth client file is a local bootstrap input, is ignored by Git, and is never copied into source, tests, logs, or committed configuration. After authorization, Eva stores an authorized-user credential document containing the client ID, client secret, refresh token, and token endpoint as a new Secret Manager version. Access tokens are refreshed in memory and are not persisted.

The credential interface exposes only load and store operations keyed by a secret reference. Application services depend on this interface rather than the Google Secret Manager client directly. Test implementations keep synthetic credentials in memory.

An OAuth revocation or `invalid_grant` marks the ConnectorAccount `REAUTHORIZATION_REQUIRED`, records a sanitized reason, and stops hot retries. Reauthorization writes a new secret version and resumes synchronization from the existing durable cursor or the bounded recovery flow.

## Persistence Model

### ConnectorAccount

ConnectorAccount contains:

- UUID primary key
- `user_id` and `workspace_id`
- provider, fixed to `gmail` for this connector
- normalized account identity
- granted scope list
- status: `CONNECTING`, `ACTIVE`, `REAUTHORIZATION_REQUIRED`, `DISABLED`, or `ERROR`
- Secret Manager resource reference
- connection timestamp
- optional sanitized status reason
- creation and update timestamps

`(workspace_id, provider, account_identity)` is unique. The existing composite Workspace ownership relationship enforces that `workspace_id` belongs to `user_id`. Secrets and access tokens are forbidden from all ConnectorAccount fields.

### GmailSyncState

There is exactly one GmailSyncState per Gmail ConnectorAccount. It contains:

- ConnectorAccount primary/foreign key
- last durable Gmail history ID
- watch expiration timestamp
- last notification timestamp
- last successful synchronization timestamp
- next watch renewal timestamp
- next safety-sync timestamp
- optional synchronization claim ID and lease expiration
- creation and update timestamps

The synchronization claim is a bounded database lease. It serializes cursor advancement for an account without holding a database transaction open during Gmail network calls. An expired claim is reclaimable after a worker crash. Completion and release require the matching claim ID so a stale worker cannot overwrite newer state.

The history ID is represented as a decimal string because Gmail defines it as an opaque monotonically increasing identifier whose size and continuity should not be assumed by application code.

## OAuth Bootstrap and Initial Watch

`connect-gmail` performs this sequence:

1. Resolve the target User and Workspace from explicit local configuration or command arguments; never derive ownership from OAuth-returned content.
2. Load the local OAuth desktop-client input.
3. Generate a state value and run the localhost authorization callback.
4. Request offline `gmail.readonly` authorization and validate returned state.
5. Sample the local lower-bound timestamp, call Gmail profile, obtain both the authorized account identity and its current history ID, and confirm the identity matches the intended account.
6. Store the authorized-user credentials in Secret Manager.
7. Upsert ConnectorAccount metadata in `CONNECTING` state without exposing credential values.
8. In one short PostgreSQL transaction, store the verified profile history ID in the existing nullable initial cursor and the sampled timestamp in `connected_at`. Preserve both values on every retry.
9. Call `users.watch` with `labelIds=["INBOX"]`, include behavior, and topic `projects/evaai-507018/topics/eva-gmail-notifications`.
10. Store the watch expiration and scheduling state, then mark the connector `ACTIVE` without replacing the durable profile cursor.

The profile cursor and timestamp form a conservative durable lower boundary before the non-transactional watch side effect. History before the profile cursor remains excluded; mail arriving during profile-to-watch setup is eligible as post-connection mail. If watch activation crashes or its transaction fails, a retry preserves that lower boundary and only refreshes external watch/scheduling state. Gmail's immediate watch notification races a persisted `CONNECTING` connector and is negative-acknowledged until activation. This uses the existing nullable initial cursor safely and requires no schema field or migration.

The command is safely repeatable. Reconnecting the same Workspace, provider, and account updates credentials and watch state rather than creating a second connector.

## Notification Validation and Acknowledgement

The pull subscriber validates the Pub/Sub envelope before provider work:

- message data exists as bytes from the Google client and decodes to a JSON object; Pub/Sub's base64 wire representation is decoded by the client adapter
- `emailAddress` is a non-empty string
- `historyId` is either an ASCII decimal string or a non-negative JSON integer and is normalized immediately to the internal decimal string form
- the email address maps to one active ConnectorAccount

Pub/Sub delivery metadata is not trusted to establish Eva's User or Workspace. Ownership always comes from the persisted ConnectorAccount.

Acknowledgement rules:

- Valid notification and successful durable synchronization: acknowledge.
- Duplicate or already-covered history notification: acknowledge.
- Transient Gmail, Secret Manager, Pub/Sub, or database failure: negative-acknowledge so Pub/Sub retries.
- Malformed or unknown-account notification: record a redacted structured warning and acknowledge to prevent a poison loop.
- Known connector still in `CONNECTING`: negative-acknowledge so initial-watch activation can finish.
- Connector requiring reauthorization: keep its cursor unchanged, record the notification time, and acknowledge. Reauthorization recovery reads from the durable cursor rather than relying on retained notifications.

The subscriber callback delegates one bounded attempt to the synchronizer. It does not contain normalization or persistence logic.

## Gmail History Synchronization

For a valid notification, the synchronizer:

1. Acquires the connector's synchronization lease in a short transaction.
2. Loads credentials through the secret boundary.
3. Calls `users.history.list` from the persisted history ID, using `historyTypes=messageAdded` and following every page token.
4. Collects message IDs from `messagesAdded`, removing duplicates within the attempt.
5. Fetches each message in full format.
6. Rechecks that the current message has the `INBOX` label and was received at or after `connected_at`.
7. Normalizes and ingests each qualifying message through EventService.
8. After every discovered Event is durable, advances the stored cursor to the response's final history ID and records synchronization health in a short transaction protected by the claim ID.
9. Releases the lease and allows the transport to acknowledge the notification.

The notification's history ID is a wake-up hint, not a cursor to assign directly. Out-of-order and duplicate notifications therefore cannot move state backward or skip history.

If the process crashes after creating some Events but before advancing the cursor, Pub/Sub redelivery repeats the history range. Event idempotency absorbs already-created messages, then synchronization completes and advances the cursor.

## Event Normalization

Each qualifying Gmail message becomes a schema-version-1 NewEvent with:

- `source`: `gmail`
- `type`: `email.received`
- `external_id`: Gmail message ID
- `idempotency_key`: `gmail:{connector_account_id}:{message_id}:received`
- `occurred_at`: Gmail internal message timestamp in UTC
- `principal_type`: the existing external/provider principal type used by the Event model
- `principal_id`: ConnectorAccount ID
- `actor`: sender name and normalized sender address when parseable
- `subject`: `{ "type": "email", "id": message_id }`
- `correlation_keys`: `["gmail-thread:{thread_id}"]`
- `metadata`: connector account ID, Gmail thread ID, history context, label IDs, and normalization schema details
- `payload`: normalized message content described below

The payload contains:

- message ID and thread ID
- From, To, Cc, Bcc, Reply-To, Date, Message-ID, and Subject headers
- Gmail snippet
- label IDs
- normalized plain-text body when present
- decoded HTML body when present, stored as untrusted data and never rendered without a separate output sanitizer
- attachment metadata: filename, MIME type, size, and Gmail attachment ID

The normalizer traverses nested MIME multiparts, decodes URL-safe base64 bodies, and applies declared character sets with safe fallbacks. It prefers an explicit `text/plain` part for plain text and retains HTML separately. Inline textual parts may contribute to the readable body; binary and attachment parts are represented only as metadata. No attachment-content API call is made.

Header absence or malformed MIME does not discard an otherwise identifiable message. The normalizer emits the best safe representation available and records bounded normalization warnings in metadata without embedding exception traces.

Provider-supplied text remains data. No email header or body can change connector scope, Event ownership, policy, approval, or application instructions.

## Dedupe and Transaction Boundaries

Event uniqueness remains the Milestone 1 `(workspace_id, idempotency_key)` constraint. One Gmail message therefore creates at most one `email.received` Event in a Workspace, even when:

- Pub/Sub redelivers a notification
- Gmail returns the same message in multiple history records or pages
- a worker crashes between Event persistence and cursor advancement
- safety synchronization overlaps ordinary notification processing
- history-gap recovery scans a message already ingested

Each Event, EventProcessing record, and Outbox row is created atomically through the existing EventService. Gmail network calls occur outside database transactions. Cursor advancement is a separate short transaction after all Events in the range are durable; replay bridges the intentional boundary between those transactions.

## Watch Renewal and Repair

Gmail watches expire and must be renewed at least every seven days. Gmail recommends daily renewal. GmailSyncState persists the next renewal time, and an idempotent maintenance service renews active connectors daily. A renewal updates only watch expiration and scheduling state; it never replaces the durable history cursor with the renewal response, which could skip unsynchronized changes. Worker startup also runs due maintenance so a restarted local process repairs a missing or near-expiry watch.

The same due-work service performs a safety history synchronization after prolonged notification silence. This is a repair loop, not the primary ingestion strategy. Due timestamps are persisted; the implementation does not rely on a long in-process sleep for correctness.

When `users.history.list` returns HTTP 404 for an expired history ID, Eva performs bounded recovery:

1. Establish a replacement Gmail watch and retain its cursor, expiration, and renewal schedule as the upper cutover candidate.
2. List inbox message IDs whose Gmail internal timestamp is at or after ConnectorAccount `connected_at`.
3. Fetch and normalize those messages through the ordinary path.
4. Let Event idempotency discard messages already persisted.
5. After every recovery Event is durable, claim-protected commit the replacement cursor together with watch expiration, renewal scheduling, and safety-sync scheduling.

Notifications racing the scan observe the active claim, are negative-acknowledged as busy, and later replay from the committed cutover. This recovers messages that arrived while Eva was connected without importing pre-connection history. A recovery failure leaves the old durable cursor unchanged and retryable even though the external replacement watch may already exist.

## Errors, Retries, and Logging

Transient provider failures include HTTP 408/429 responses, Gmail's reason-coded rate-limit 403 responses, server errors, deadlines, and temporary credential-service or network failures. Gmail calls use an actual `httplib2` socket timeout plus a bounded total-attempt loop with exponential backoff and symmetric jitter; `asyncio.to_thread()` keeps blocking work off the event loop but is not the timeout mechanism. Exhaustion returns a fixed retryable failure to Pub/Sub or the one-shot command. `invalid_grant`/revocation and expired history remain distinct permanent/recovery classifications and do not consume the transient retry loop.

Permanent authorization failures transition the connector to `REAUTHORIZATION_REQUIRED`. Unparseable individual messages do not advance the cursor silently: the attempt fails unless the normalizer can produce the minimum canonical representation containing a valid Gmail message ID and timestamp.

Structured logs may contain:

- ConnectorAccount ID
- Workspace ID
- hashed or redacted account identity
- Pub/Sub message ID
- Gmail message and thread IDs
- cursor/claim identifiers
- operation, attempt, latency, and outcome category
- sanitized exception class and bounded summary

Logs never contain OAuth codes, client secrets, refresh/access tokens, raw authorization payloads, email bodies, subjects, full address lists, or attachment contents.

## Configuration

Typed settings cover:

- Google Cloud project ID
- Gmail notification topic ID
- Gmail pull subscription ID
- OAuth bootstrap client-file path, local-only and optional outside the command
- synchronization lease duration
- watch renewal lead time
- safety-sync interval
- Gmail socket request timeout (`gmail_request_timeout_seconds`, default 30)
- Gmail total request attempts (`gmail_retry_attempts`, default 3)
- Gmail initial/max exponential backoff (`gmail_retry_initial_backoff_seconds`, default 0.5; `gmail_retry_max_backoff_seconds`, default 8)
- Gmail symmetric jitter ratio (`gmail_retry_jitter_ratio`, default 0.2, constrained to 0–1)

No refresh token, access token, OAuth code, or client secret is accepted as a normal environment setting. Application Default Credentials authorize Eva to Pub/Sub and Secret Manager; user OAuth credentials authorize access to the Gmail mailbox.

## Testing Strategy

### Unit tests

Unit tests cover:

- OAuth state, account-identity validation, repeat connection, and secret redaction
- Pub/Sub message decoding and validation
- history pagination and duplicate message collection
- out-of-order and already-covered notifications
- MIME traversal, base64url decoding, character-set fallback, alternative bodies, inline parts, and malformed content
- header and address normalization
- INBOX and `connected_at` filtering
- Event field mapping and idempotency-key construction
- transient retry classification and permanent authorization classification
- socket-timeout transport construction, retry exhaustion, deterministic backoff/jitter, and cancellation
- cancellation-safe/concurrent lazy Secret Manager and Pub/Sub client construction plus retry-safe cleanup ownership
- truthful one-shot connect/sync/maintain result and process-exit matrices
- log/error sanitization
- watch-renewal and safety-sync due-time decisions with an injected clock
- expired-history bounded recovery with watch-before-scan cutover injection

### PostgreSQL integration tests

Integration tests cover:

- migration upgrade and ownership constraints
- ConnectorAccount uniqueness and secret-reference-only persistence
- one-to-one GmailSyncState behavior
- exclusive synchronization claims and expired-lease recovery
- claim-protected cursor completion and release
- Event + EventProcessing + Outbox creation from normalized Gmail input
- notification replay without duplicate Events or Outbox rows
- crash-style replay where Events exist but the Gmail cursor was not advanced
- concurrent ordinary and repair synchronization for one connector
- reauthorization status transitions

### Adapter contract tests

Google clients are injected behind focused interfaces. Contract tests use fakes to assert exact Gmail watch/history/message requests, Pub/Sub acknowledge decisions, and Secret Manager serialization without network access. The normal automated suite never reads the personal mailbox or real credentials.

### Live smoke test

The documented manual smoke test uses `saswatray2505@gmail.com` and the real GCP resources:

1. Complete OAuth consent and connect the mailbox.
2. Start the local pull worker.
3. Send one plain-text and one HTML inbox message, with at least one carrying attachment metadata.
4. Confirm exactly one durable `email.received` Event per Gmail message.
5. Confirm labels, bodies, correlation keys, and attachment metadata are normalized as designed.
6. Replay or redeliver a notification and confirm no additional Event or Outbox row.
7. Restart the worker, send another message, and confirm synchronization resumes from PostgreSQL state.
8. Inspect logs to confirm sensitive content is absent.

## Acceptance Criteria

Milestone 2 is complete when:

- a newly arriving message with `INBOX` becomes a canonical durable `email.received` Event through Gmail watch and Pub/Sub without primary polling
- messages received before initial connection are not imported
- Primary, Promotions, Social, and Updates messages are eligible and retain their Gmail labels
- headers, plain text, HTML, and attachment metadata are normalized; attachment files are not downloaded
- every Gmail Event has the approved source, type, external ID, idempotency key, principal, subject, and Gmail-thread correlation key
- duplicate and out-of-order notifications do not duplicate Events or move the cursor backward
- worker crashes and restarts replay safely from PostgreSQL state
- watch renewal, notification-silence repair, expired-history recovery, and reauthorization-required behavior are implemented and tested
- PostgreSQL never stores OAuth credential values and logs contain no email content or tokens
- automated tests pass and the live smoke test succeeds
- relevant documentation explains OAuth Console setup, GCP resource setup, local operation, recovery, and teardown
- the feature branch is committed, pushed, and delivered as a GitHub pull request for review

## References

- [Configure push notifications in Gmail API](https://developers.google.com/workspace/gmail/api/guides/push)
- [Gmail `users.watch`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/watch)
- [Gmail `users.history.list`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list)
- [Choose Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [Using OAuth 2.0 to access Google APIs](https://developers.google.com/identity/protocols/oauth2)
- [When OAuth verification is not needed](https://support.google.com/cloud/answer/13464323)
