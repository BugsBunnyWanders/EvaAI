# Milestone 4 Relevance Engine Design

**Date:** 2026-09-01
**Status:** Approved for implementation planning

## Objective

Add Eva's first goal-aware relevance engine. Milestone 4 evaluates immutable Events through
conservative deterministic filters and, when needed, a bounded structured AI classifier. The
application then maps the validated result to one of four dispositions: `IGNORE`, `RECORD`,
`NOTIFY`, or `INVESTIGATE`.

The milestone also introduces the generic Signal primitive. Every successful relevance
evaluation becomes a versioned Signal linked to its source Event, selected Goals, and any
resulting Situation. Events and superseded Signals remain available so a later explicit
re-evaluation can reach a different decision without losing history.

Milestone 4 decides what deserves further attention. It does not send Telegram messages, run a
full investigating agent, or execute actions.

## Decisions

- Use a durable inline evaluation pipeline behind the existing `EventHandler` boundary.
- Keep deterministic filtering, context construction, classification, routing, and persistence
  behind separate interfaces so they can become independent workers later.
- Add a provider-neutral classifier interface, a real OpenAI Responses API adapter, and scripted
  deterministic test implementations.
- Use configurable `gpt-5.6-luna` as the default relevance model.
- Treat classifier output as advisory. Versioned application code owns the final disposition.
- Retain every Event and every successful relevance decision. Re-evaluation creates a new Signal
  and supersedes the previous one.
- Add explicit re-evaluation now. Defer automatic rescans when Goals or classifier configuration
  change.
- Use conservative deterministic filtering. Gmail Promotions, Social, and newsletter-like
  messages are not ignored merely because of a provider category.
- Fail safely: classifier failures never default to `IGNORE` and never create a Situation.
- Send only bounded, normalized context to the model and use `store=False`.
- Automatically evaluate new Events only while relevance processing is enabled. Provide a
  bounded operator command for unevaluated historical Events; never start an implicit full-history
  scan.
- Develop on `codex/milestone-4-relevance-engine`, push the branch, and deliver through a pull
  request to `main`.
- Add comments around non-obvious privacy, transaction, idempotency, concurrency, and
  supersession invariants. Avoid comments that merely restate code.

## Alternatives Considered

### Durable inline pipeline — selected

The current Event processor invokes one orchestrating relevance handler. The handler runs the
stages in order and persists durable results. This provides retries, auditability, and proactive
processing without adding deployment units.

### Separate asynchronous stages

Screening, classification, and routing could each have their own topic and worker. That structure
scales independently but adds queue contracts, more failure states, and more operations than the
current single-user deployment needs. The selected interfaces preserve this migration path.

### On-demand classification

Eva could classify an Event only when a later user query needs it. This is operationally simpler
but weakens proactivity, durable audit history, and predictable reconsideration, so it does not
satisfy this milestone.

## Scope

Milestone 4 includes:

- generic versioned Signal persistence
- relevance evaluation-attempt persistence
- deterministic screening and composable ignore rules
- bounded Goal- and Situation-aware context construction
- provider-neutral structured classifier contracts
- OpenAI Responses API classifier adapter
- deterministic fake and scripted classifier implementations
- application-owned disposition policy with versioned thresholds
- routing to `IGNORE`, `RECORD`, `NOTIFY`, or `INVESTIGATE`
- Situation creation or reuse for `NOTIFY` and `INVESTIGATE`
- explicit Event re-evaluation with an operator reason
- bounded backfill of Events that have no current relevance Signal
- CLI inspection of current and historical relevance decisions
- Alembic migration, unit tests, PostgreSQL integration tests, and operator documentation

Milestone 4 excludes:

- Telegram delivery or real-time chat changes
- full-agent investigation or autonomous actions
- automatic re-evaluation after Goal, prompt, model, or policy changes
- automatic full-history backfills
- attachment content extraction
- embeddings, vector retrieval, or semantic Situation correlation
- inferred Goal creation
- end-user rule-management UI
- public relevance HTTP endpoints
- multi-user Gmail onboarding

## Evaluation Flow

```text
Immutable Event
      |
      v
Deterministic screening
      |
      v
Bounded context builder
(Event + active Goals + candidate Situations)
      |
      v
Provider-neutral classifier
(OpenAI or deterministic test implementation)
      |
      v
Strict result validation
      |
      v
Versioned application routing policy
      |
      v
Versioned Signal
      |
      +-- IGNORE / RECORD ---------> handled
      |
      +-- NOTIFY / INVESTIGATE ----> create or reuse Situation -> handled
```

The Event is immutable throughout. A deterministic filter may produce an `IGNORE` Signal without
calling a model. Every other successful path produces a validated relevance Signal before the
Event is considered handled.

## Domain Types

### Disposition

`RelevanceDisposition` has exactly four values:

- `IGNORE`: retain Event and Signal, but take no further action
- `RECORD`: retain Event and Signal as queryable context, but create no Situation
- `NOTIFY`: create or update a Situation for a later notification stage
- `INVESTIGATE`: create or update a Situation for a later investigating-agent stage

### Classifier Result

The provider-neutral `ClassifierResult` is an immutable, strict Pydantic model containing:

- `relevance`, `importance`, `urgency`, and `confidence`, each from 0 through 1
- `category`
- advisory `recommended_action`
- concise `reason`, limited to 1,000 characters
- at most five `goal_matches`

The initial category enum is:

- `GOAL_PROGRESS`
- `REQUEST_OR_COMMITMENT`
- `DEADLINE`
- `RISK_OR_SECURITY`
- `FINANCIAL`
- `TRAVEL`
- `PERSONAL`
- `INFORMATIONAL`
- `PROMOTIONAL`
- `SPAM`
- `OTHER`

The advisory action enum uses the same four values as `RelevanceDisposition`, but it is not an
authorization or final routing decision.

Each goal match contains a supplied Goal ID, relevance from 0 through 1, contribution
(`SUPPORTS`, `BLOCKS`, or `CONTEXT`), and a bounded reason. Application validation rejects IDs that
were not present in the classifier input, repeated IDs, excessive matches, and cross-scope Goals.

### Relevance Signal Payload

The typed JSON payload is a discriminated union on `method`:

- `AI` contains the validated classifier result plus the application-selected disposition.
- `DETERMINISTIC` contains the exact filter reason code, confidence 1, and disposition `IGNORE`.

This keeps deterministic decisions auditable without inventing classifier scores or categories.
Neither variant contains source content or prompts.

## Persistence Model

### Signal

`signals` stores a successful derived fact:

- UUID primary key
- `user_id`, `workspace_id`, and `event_id`
- kind; Milestone 4 creates `RELEVANCE`
- positive schema version
- typed JSONB payload containing the validated classifier result or deterministic-filter result
- extracted confidence and final disposition for indexed queries
- producer (`DETERMINISTIC` or `AI`)
- provider and optional model name
- classifier version and routing-policy version
- SHA-256 digest of the canonical bounded input, not the input or prompt itself
- trigger (`INITIAL`, `EXPLICIT_REEVALUATION`, or `BACKFILL`)
- optional bounded operator reason for an explicit re-evaluation
- evaluation idempotency key
- optional successful evaluation-attempt ID
- optional resulting Situation ID
- optional `supersedes_signal_id`
- `is_current`
- creation timestamp

Composite foreign keys require the Signal, Event, optional predecessor, optional attempt, and
optional Situation to share the same User and Workspace. A partial unique index permits only one
current relevance Signal for an Event in a tenant. A separate scoped uniqueness constraint on the
evaluation key makes retries of the same logical request idempotent.

Supersession is append-only except for changing the previous Signal's `is_current` flag. In one
transaction, the repository locks the current Signal, inserts the replacement, marks the previous
record non-current, and verifies that the replacement points to that exact predecessor. It rejects
self-links, branches, and scope changes.

Reusing an idempotency key returns the Signal originally created for that logical request. If that
Signal has since been superseded, replaying the old request does not make it current again.

### SignalGoal

`signal_goals` records validated Goal matches:

- Signal, Goal, User, and Workspace IDs
- relevance from 0 through 1
- contribution type
- bounded reasoning

Its composite primary and foreign keys prevent duplicate or cross-tenant relationships. The
selected Goal IDs are passed to Situation resolution when the final disposition requires a
Situation.

### RelevanceEvaluationAttempt

`relevance_evaluation_attempts` records each external classifier invocation:

- UUID primary key
- Event, User, and Workspace IDs
- logical evaluation idempotency key and positive attempt number
- trigger: `INITIAL`, `EXPLICIT_REEVALUATION`, or `BACKFILL`
- optional operator reason, limited to 500 characters
- status: `STARTED`, `SUCCEEDED`, `RETRYABLE_FAILURE`, or `PERMANENT_FAILURE`
- provider, model, classifier version, and canonical-input digest
- sanitized failure code and no raw exception text
- start and completion timestamps

The pair `(evaluation_key, attempt_number)` is unique within tenant scope. Attempt rows contain no
email body, prompt, credentials, or raw model output. Deterministic screening does not create an
external-attempt row; its Signal provenance is sufficient.

## Deterministic Screening

Screening runs before any paid model call. Filters implement a small composable protocol and return
either no decision or a deterministic reason code. The initial filter set covers:

- a current relevance Signal for the same initial or backfill operation: idempotent no-op
- duplicate Events that escaped upstream provider idempotency
- malformed or unsupported Events
- explicitly muted source or event type
- explicitly ignored sender or Gmail label

A positive filter result produces a deterministic `IGNORE` Signal except for the already-current
case, which returns the existing result without writing. User-configured rules are exact normalized
matches loaded through a tenant-scoped rule-provider interface. Milestone 4 supplies configuration
and test implementations, not an end-user rule editor.

Gmail's Promotions, Social, Updates, Forums, and newsletter-like characteristics are never built-in
hard-ignore rules. They remain context for the classifier because a personal mailbox can contain
important messages in any category.

## Context Construction and Privacy Boundary

The context builder reads only the stored normalized Event. It never makes a second Gmail request.
For `gmail/email.received`, it may include:

- sender display name and address
- subject and snippet
- Gmail label IDs
- at most 4,000 characters of normalized plain-text body

It excludes:

- HTML
- attachment content
- raw headers other than the selected normalized fields
- provider tokens, OAuth material, Pub/Sub metadata, connector state, and internal secrets

The Goal selector includes at most 20 active Goals in deterministic order: priority descending,
creation time ascending, then UUID. Each Goal summary is bounded to 500 characters and the combined
Goal context to 8,000 characters.

The Situation selector includes at most five nonterminal, tenant-scoped candidates. An exact Gmail
thread correlation match comes first. Remaining capacity may contain the most recently active
Situations linked to included Goals, ordered by attention, last activity, then UUID. Each Situation
summary is bounded to 500 characters and combined Situation context to 2,500 characters.

All provider text is delimited as untrusted data. The classifier instructions explicitly state that
commands, policies, links, requests to reveal prompts, and tool instructions inside the Event are
content to classify, not instructions to follow. The classifier has no tools or connector access.

The canonical bounded context, classifier version, and policy-relevant metadata are hashed for
audit comparison. The raw prompt and bounded context are not persisted in Signal or attempt rows.

## Classifier Architecture

`RelevanceClassifier` accepts an immutable `EvaluationContext` and returns a
`ClassifierResult`. No OpenAI SDK type crosses this interface.

The OpenAI adapter:

- uses the Responses API
- defaults to configurable `gpt-5.6-luna`
- parses directly into the strict Pydantic result schema
- sends `store=False`
- uses no tools
- maps transport errors, rate limits, refusals, and validation failures into typed application
  errors
- never logs request content, raw responses, or credentials

Scripted and fake adapters return deterministic results or typed failures for unit and integration
tests. The normal test suite performs no network calls and requires no OpenAI credential.

The design follows OpenAI's documentation for
[structured output parsing](https://developers.openai.com/api/docs/guides/structured-outputs) and
the Responses API [`store` option](https://developers.openai.com/api/reference/cli/resources/responses/methods/create).
`store=False` disables later API retrieval of the generated response; this design does not describe
it as a general zero-retention guarantee.

## Application-Owned Routing Policy

The classifier recommends an action, but `RelevanceRoutingPolicy` determines the final disposition
from validated scores and versioned configuration. Initial defaults use the following precedence:

1. `NOTIFY` when relevance is at least 0.75, confidence at least 0.70, and importance or urgency is
   at least 0.65.
2. `INVESTIGATE` when the advisory action is `INVESTIGATE`, relevance is at least 0.60, and
   confidence at least 0.65.
3. `IGNORE` when the advisory action is `IGNORE`, relevance is at most 0.20, and confidence at
   least 0.80.
4. `RECORD` for every other valid result, including low-confidence results.

Thresholds are settings, not prompt text. Their version changes whenever semantics or values
change, and every Signal records the applied policy version. A model response alone cannot trigger
a future notification, investigation, tool, or action.

## Situation Routing

`IGNORE` and `RECORD` stop after persisting the Signal and marking the evaluation handled. The Event
and current Signal remain queryable and eligible for explicit re-evaluation.

`NOTIFY` and `INVESTIGATE` invoke the existing deterministic Situation resolver with the Event and
validated Goal matches. Gmail Events reuse the Workspace's `gmail-thread:` correlation key, so
multiple relevant messages in one conversation converge on one Situation. The new Signal stores
the resulting Situation ID.

The relevance handler does not deliver a notification or run an investigator. Later consumers must
join through the current Signal and ignore superseded dispositions.

If re-evaluation changes `NOTIFY` or `INVESTIGATE` to `RECORD` or `IGNORE`, Eva does not delete the
old Situation, remove historical Event links, or automatically change its lifecycle. The old Signal
continues to explain why that Situation was created but is no longer current. This prevents one
re-evaluated Event from closing a Situation that contains other Events or user activity.

## Processing, Transactions, and Retries

The relevance evaluator implements the existing `EventHandler` boundary. Network I/O never occurs
inside a long-lived database transaction:

1. Claim the Event and create a `STARTED` attempt for AI evaluation.
2. Load tenant-scoped bounded context and call the classifier outside a database transaction.
3. Validate the result, compute the application disposition, and mark the external attempt
   successful without storing the raw result.
4. In one short routing transaction, persist the Signal and Goal links, supersede any previous
   Signal, and advance Event processing to `CLASSIFIED`.
5. In that same transaction, `NOTIFY` and `INVESTIGATE` resolve and link the Situation and advance
   processing to `CORRELATED`.
6. Mark processing `HANDLED` before committing the routing transaction.

Deterministic `IGNORE` paths omit steps 1 and 2 but use the same Signal and handling transaction.
Uniqueness constraints and current-Signal row locking make concurrent retries converge on one
result. A routing-transaction failure rolls back the Signal, Goal links, Situation changes, and
processing-stage changes together.

Timeouts, transient transport failures, and rate limits are retryable. Defaults are three external
attempts, exponential backoff starting at two seconds, a 30-second cap, and jitter. Refusals and
repeated invalid structured output become permanent after the configured bound. Every failed
attempt records only a sanitized code, releases the Event claim according to the existing processor
contract, and creates neither Signal nor Situation. An exhausted Event remains available for
explicit operator re-evaluation; it is never silently converted to `IGNORE`.

If a worker dies after creating a `STARTED` attempt, stale-claim recovery marks that attempt as a
retryable `INTERRUPTED` failure before creating the next numbered attempt.

## Initial Evaluation, Re-evaluation, and Backfill

### Initial processing

While relevance processing is enabled, newly ingested Events flow through the relevance handler.
The initial logical evaluation key is stable for that Event. If any current relevance Signal already
exists, changing model, prompt, Goal, or policy configuration does not automatically evaluate the
Event again.

### Explicit re-evaluation

The service accepts explicit User, Workspace, and Event IDs, a new caller-visible idempotency key,
and a required non-blank operator reason. It reloads the original Event, rebuilds context using
current Goals and Situations, runs current classifier and policy versions, and supersedes the
previous Signal only after a successful result. Reusing the same idempotency key returns the same
logical outcome; a deliberate new decision uses a new key.

### Bounded backfill

The operator backfill command selects only Events in one explicit tenant scope that have no current
relevance Signal. It uses deterministic occurrence-time and UUID ordering, defaults to 50 Events,
and rejects limits above 100. Each Event gets its own stable backfill evaluation key, so rerunning a
partially completed batch skips completed Events.

No startup hook or migration launches a backfill. Existing Events, including Gmail test Events, are
evaluated only when the operator explicitly runs the command.

## CLI Surface

Milestone 4 adds stable JSON commands for operators:

- `eva relevance show` returns the current relevance Signal for one scoped Event
- `eva relevance history` returns its Signal and evaluation-attempt history
- `eva relevance reevaluate` requests a new decision with a required reason
- `eva relevance backfill` evaluates a bounded batch with no current relevance Signal

Mutating commands require explicit User, Workspace, and Event scope as applicable. Output includes
IDs, versions, dispositions, scores, reason, linked Goals and Situation, attempt status, and
sanitized failure codes. It never prints email bodies, prompts, credentials, or raw responses.
`reevaluate` accepts an optional idempotency key; when omitted, the CLI generates and reports one
before processing so an uncertain retry can reuse it.

## Configuration

Settings cover:

- relevance enabled flag, disabled by default for backward-compatible startup
- provider, default `openai` when enabled
- OpenAI API key, required only when the enabled provider is OpenAI
- model, default `gpt-5.6-luna`
- classifier and routing-policy versions
- Event body, Goal, and Situation context bounds
- disposition thresholds
- retry count and backoff values
- exact ignored sources, event types, senders, and labels

Secrets use Pydantic secret types and are never represented in logs or validation messages. Local
environment variables support development. Deployed values come from Secret Manager-backed service
environment configuration.

## Failure Semantics

- Unsupported or explicitly muted Event: deterministic current `IGNORE` Signal.
- Existing current Signal during initial processing or backfill: idempotent no-op.
- Context scope mismatch: permanent typed failure, no Signal or Situation.
- OpenAI timeout, rate limit, or transient transport error: bounded retry.
- Refusal or invalid structured output: bounded typed failure, then operator review.
- Database conflict during success persistence: complete routing-transaction rollback and
  idempotent retry.
- Situation resolution failure: complete routing-transaction rollback; no Signal, Situation
  change, or handled outcome is committed.
- Re-evaluation failure: old Signal remains current and authoritative.

No failure path interprets uncertainty as irrelevance.

## Security and Isolation

- Every repository operation requires explicit User and Workspace IDs.
- Composite foreign keys are the final ownership boundary for Events, Signals, Attempts, Goals, and
  Situations.
- Classifier-selected Goal IDs must be a subset of the supplied tenant-scoped candidates.
- Provider content cannot choose ownership, policy versions, dispositions, lifecycle, or tool
  authority.
- Logs use opaque IDs, versions, timing, disposition, and sanitized codes only.
- Signal and attempt records exclude raw prompts, email bodies, HTML, attachments, credentials, and
  raw model responses.
- Application code, not model text, owns every state transition and routing decision.

## Verification Strategy

### Unit tests

- strict domain validation and enum handling
- every deterministic filter and filter precedence
- every routing threshold boundary and policy precedence
- Gmail context normalization, ordering, and all size limits
- prompt-injection text remains delimited untrusted content
- Goal match subset and tenant validation
- OpenAI adapter request mapping, `store=False`, structured parsing, refusal, and error mapping
- fake and scripted classifier behavior
- retry classification and backoff calculation

### PostgreSQL integration tests

- migration upgrade and downgrade coverage
- scoped composite foreign keys and indexes
- one current relevance Signal per scoped Event
- idempotent evaluation keys and concurrent processing
- transactional supersession without branches
- Signal-to-Goal and Signal-to-Situation scope isolation
- attempt lifecycle and sanitized failure persistence
- all four routing outcomes
- Gmail-thread Situation reuse
- downgrade from actionable to non-actionable without history deletion
- re-evaluation failure leaves the old Signal current
- bounded backfill selects only unevaluated scoped Events

### Application and CLI tests

- EventProcessor integration and stage transitions
- initial processing, explicit re-evaluation, and backfill
- stable JSON output and exit codes
- secrets and provider content never appear in logs or CLI output
- normal tests require no network access or OpenAI credential
- full existing test, Ruff, strict mypy, migration, and CI suites remain green

An optional manually invoked live smoke test may verify configured OpenAI access, but it is not part
of the deterministic test suite and must not print mailbox content.

## Completion Criteria

Milestone 4 is complete when:

- new Events can be deterministically screened and structurally classified while relevance is
  enabled;
- every successful evaluation has one auditable current Signal;
- application policy routes only to the four approved dispositions;
- `NOTIFY` and `INVESTIGATE` create or reuse a Situation without delivering or acting;
- classifier failures retry safely and never become implicit ignores;
- explicit re-evaluation preserves full history and can change the current decision;
- bounded backfill evaluates only operator-selected unevaluated Events;
- tenant isolation, privacy bounds, idempotency, concurrency, and failure behavior are verified;
- the complete repository verification suite passes; and
- the milestone branch is pushed and delivered through a reviewed pull request to `main`.
