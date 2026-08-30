# Milestone 1 Event Backbone Design

**Status:** Approved for implementation
**Date:** 2026-08-30
**Product specification:** `spec/2026-08-29-proactive-personal-ai-agent-design.md`

## Objective

Build Eva's durable event backbone: canonical Event persistence, workspace-scoped source idempotency, transactional Outbox creation, a replaceable publishing boundary with a real Google Pub/Sub adapter, and an idempotent event-processing worker contract.

The milestone proves the reliability boundary from accepted canonical input through durable publication and worker dispatch. It does not ingest provider webhooks or interpret provider-specific payloads.

## Decisions

- Work is developed on `codex/milestone-1-event-backbone`, pushed to GitHub, and delivered through a pull request to `main`.
- The application owns the Event + EventProcessing + Outbox transaction explicitly through SQLAlchemy application services.
- Minimal `users` and `workspaces` tables are introduced so Event scope is enforced by PostgreSQL from the first domain migration.
- Event idempotency is unique within a Workspace.
- Outbox publication uses bounded claims and expiring leases; network publication never occurs while holding a database transaction open.
- A `Publisher` protocol separates the event domain from local and Google Pub/Sub implementations.
- The Google Pub/Sub adapter is implemented now, but Milestone 1 creates no GCP resources. Topic, project, and credentials remain external configuration.
- The worker transport is separate from event processing. No public Pub/Sub webhook, subscription loop, or unauthenticated internal endpoint is added.
- Code comments explain invariants, transaction boundaries, claims, acknowledgements, workspace checks, and other non-obvious behavior. Self-evident code is not narrated.

## Scope

Milestone 1 includes:

- minimal User and Workspace persistence
- canonical Event domain types and database model
- EventProcessing state and attempt history fields
- transactional Outbox model
- workspace-scoped Event idempotency
- atomic Event, EventProcessing, and Outbox creation
- internal event-available message schema
- publisher protocol and in-memory implementation
- Google Pub/Sub publisher implementation
- lease-based outbox claiming and batch publication
- worker-side EventHandler protocol and EventProcessor
- structured logs with event, workspace, outbox, and outcome identifiers
- migrations, unit tests, PostgreSQL integration tests, and CI coverage

Milestone 1 excludes:

- Gmail or any provider connector
- raw webhook verification or normalization
- relevance, Signals, Goals, Situations, Memory, or Agents
- public worker HTTP endpoints
- long-running subscriber or polling loops
- creation of Pub/Sub topics, subscriptions, IAM, or service accounts
- Terraform or Cloud Run deployment
- exponential backoff, dead-letter queues, reconciliation jobs, or stuck-work repair
- event replay APIs or administration UI

## Persistence Model

### User

The initial User record contains:

- UUID primary key
- display name
- creation and update timestamps

Authentication identity and profile expansion are deferred.

### Workspace

The initial Workspace record contains:

- UUID primary key
- owning `user_id`
- name
- creation and update timestamps

`(id, user_id)` is unique so child records can use a composite foreign key that proves a Workspace belongs to the stated User. `(user_id, name)` is also unique.

### Event

The canonical Event contains:

- UUID primary key
- `user_id` and `workspace_id`
- source and type
- optional external ID
- idempotency key
- occurred and received timestamps
- principal type and optional principal ID
- optional JSONB actor and subject
- JSONB payload and metadata
- PostgreSQL text-array correlation keys
- positive schema version

The unique key is `(workspace_id, idempotency_key)`. A composite foreign key `(workspace_id, user_id)` references the Workspace relationship, preventing cross-user scope mismatches.

Events have no update service. They are immutable factual history after insertion.

### EventProcessing

There is one processing record per Event. It contains:

- Event primary/foreign key
- current stage: `RECEIVED`, `NORMALIZED`, `ENRICHED`, `CLASSIFIED`, `CORRELATED`, or `HANDLED`
- attempt count
- sanitized last error
- optional next retry and processed timestamps
- optional worker claim ID and lease expiry
- creation and update timestamps

Milestone 1 creates records at `RECEIVED`. The worker can mark `HANDLED` only after a registered handler completes successfully.

### OutboxMessage

Each internal message contains:

- UUID primary key
- Event foreign key
- destination/topic name
- message type and schema version
- JSONB payload
- state: `PENDING`, `PUBLISHING`, or `PUBLISHED`
- attempt count and sanitized last error
- availability timestamp
- optional claim ID and lease expiry
- optional provider message ID and published timestamp
- creation and update timestamps

An Outbox row is created in the same transaction as its Event. Claim identity is required when completing or releasing a publish attempt so a stale worker cannot overwrite a newer claim.

## Canonical Commands and Messages

`NewEvent` is an immutable, validated application command. It rejects:

- blank source, type, or idempotency key
- naive datetimes
- non-positive schema versions
- non-object actor, subject, payload, or metadata values

`EventAvailableMessage` is the internal published envelope. It contains the outbox ID, event ID, user ID, workspace ID, event type, and schema version. It never embeds credentials or grants authority.

## Event Ingestion Transaction

`EventService.ingest(new_event)` owns one database transaction:

1. Attempt `INSERT ... ON CONFLICT DO NOTHING` for `(workspace_id, idempotency_key)`.
2. If the Event already exists, load it and return `created=False`.
3. For a new Event, insert EventProcessing at `RECEIVED`.
4. Create one pending Outbox message containing `EventAvailableMessage`.
5. Commit all three records together.

Duplicate deliveries create no new processing or outbox records. A database failure rolls the transaction back completely.

Workspace scope is validated by PostgreSQL constraints rather than trusted from external content.

## Outbox Publication

`OutboxRelay.publish_batch(limit)` performs one bounded pass:

1. Open a short transaction.
2. Select eligible pending or expired-lease messages using `FOR UPDATE SKIP LOCKED`.
3. Assign a unique claim ID, set `PUBLISHING`, increment attempts, and set lease expiry.
4. Commit the claims.
5. Publish each message outside the transaction through `Publisher.publish()`.
6. In a new transaction, mark a matching claim `PUBLISHED` only after acknowledgement.
7. On publication failure, record a sanitized error and return the matching claim to `PENDING`.

If a process crashes after claiming, lease expiry makes the row eligible for another relay. If it crashes after provider acceptance but before marking the row published, the message may be published again; consumers must therefore remain idempotent. This preserves the system's at-least-once contract.

Milestone 1 uses immediate future eligibility after a failed attempt. Backoff and dead-letter policy are deferred to reliability hardening.

## Publisher Boundary

The Publisher protocol accepts a typed outbound message and returns a provider message ID.

Implementations:

- `InMemoryPublisher` records messages and returns deterministic IDs for tests and local composition.
- `GooglePubSubPublisher` serializes the envelope as UTF-8 JSON, resolves the configured project/topic path, publishes through the official Google client, and awaits provider acknowledgement.

The Google adapter receives an injectable client for tests. Application Default Credentials remain the Google client's responsibility. Secrets are not added to Eva settings or source control.

## Worker Processing Contract

`EventProcessor.process(message, handler)` performs one message attempt:

1. Validate the internal envelope.
2. Load the Event and EventProcessing record.
3. Verify the message's user and workspace match the persisted Event.
4. If processing is already `HANDLED`, return an idempotent already-handled result without invoking the handler.
5. Claim the processing row with a unique claim ID and bounded lease, incrementing the attempt count in a short transaction.
6. If another unexpired claim owns the row, return a retryable busy result without invoking the handler.
7. Invoke the provided EventHandler outside any long-held database lock.
8. On success, update the matching claim to `HANDLED`, clear the lease, and set `processed_at`.
9. On failure, store a sanitized error, release the matching claim, leave the stage unchanged, and re-raise for transport-level retry.

An expired worker lease is reclaimable after a crash. Claim-protected completion prevents an older worker from overwriting a newer attempt.

The worker module provides construction and dispatch interfaces but no infinite loop. Milestone 2 can supply Gmail-specific handlers without changing this contract.

## Error and Safety Rules

- Stored errors contain the exception class plus a bounded, sanitized summary.
- Database URLs, credentials, tokens, and full stack traces are never stored in EventProcessing or Outbox rows.
- Unknown Event IDs and scope mismatches fail without invoking handlers.
- Outbox completion and release operations require the current claim ID.
- Event processing completion and release operations require the current worker claim ID.
- Publication failures never delete Outbox rows.
- Handler failures never mark processing handled.
- Uncertain publication outcomes remain compatible with duplicate delivery; exact-once publication is not claimed.

## Configuration

New typed settings cover:

- Pub/Sub project ID, optional until the Google adapter is selected
- Pub/Sub topic ID
- default outbox batch limit
- outbox lease duration

No GCP credentials are represented in application settings. Google authentication uses the environment's Application Default Credentials.

## Observability

Structured logs record relevant identifiers:

- `event_id`
- `user_id`
- `workspace_id`
- `outbox_message_id`
- claim ID where relevant
- processing or publication outcome

Event payload bodies are not logged by default. Comments document why deduplication, claim checks, and transaction boundaries exist.

## Testing Strategy

Unit tests cover:

- canonical command validation
- internal envelope serialization
- error sanitization
- in-memory publisher behavior
- Google Pub/Sub serialization, topic resolution, and acknowledgement
- processor success, duplicate handling, scope mismatch, and failure behavior

PostgreSQL integration tests cover:

- clean migration and constraints
- workspace/user scope enforcement
- atomic Event + EventProcessing + Outbox persistence
- sequential and concurrent duplicate ingestion
- rollback on transaction failure
- exclusive claims across relay instances
- expired lease reclamation
- claim-protected completion and release
- publish success and failure outcomes
- exclusive processing claims and expired processing-lease reclamation
- handler failure followed by successful reprocessing

Provider network calls are faked at the Google client boundary. No test creates or requires a real GCP resource.

## Completion Criteria

Milestone 1 is complete when:

1. A canonical Event can be persisted with its processing and outbox records atomically.
2. Duplicate and concurrent duplicate ingestion produce one durable Event and one Outbox message.
3. A batch relay safely claims, publishes, acknowledges, releases, and reclaims expired work.
4. The same Publisher contract works with in-memory and Google Pub/Sub implementations.
5. A worker processor dispatches once, handles re-delivery idempotently, and preserves failure history.
6. PostgreSQL enforces User/Workspace/Event scope relationships.
7. Unit and integration tests, Ruff, strict mypy, migrations, and local smoke checks pass.
8. GitHub Actions passes on the feature branch.
9. The branch is pushed and a pull request to `main` is created with verification evidence.
