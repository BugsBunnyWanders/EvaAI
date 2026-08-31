# Milestone 3 Goal and Situation Design

**Date:** 2026-08-31
**Status:** Approved for implementation planning

## Objective

Add Eva's first durable intent and operational-context domain. Milestone 3 introduces Goals, Situations, current Situation snapshots, explicit Goal-to-Situation and Event-to-Situation relationships, and deterministic Gmail-thread correlation.

The milestone prepares stable interfaces for relevance, memory, agent reasoning, and Telegram without implementing those later layers. Gmail Events are not automatically routed into Situations yet. Milestone 4 will decide whether an Event is relevant and invoke the resolver only when a Situation should be created or updated.

## Decisions

- Use normalized PostgreSQL tables and focused domain services rather than a JSON aggregate or a full event-sourcing framework.
- Develop on `codex/milestone-3-goal-situation`, push the branch, and deliver through a pull request to `main`.
- Keep Goal and Situation ownership scoped by the existing User and Workspace relationship, with PostgreSQL constraints as the final security boundary.
- Support explicit and inferred Goal metadata now, but add no inference behavior. Operator-created Goals are `USER_EXPLICIT` and begin `ACTIVE`; `AGENT_INFERRED` Goals begin `CANDIDATE`.
- Treat Goal autonomy metadata as descriptive input only. Milestone 3 grants no action authority; future policy infrastructure remains authoritative.
- Use a dedicated correlation-key table so strong provider identifiers can be reserved atomically and exactly once within a Workspace.
- Support only deterministic Gmail-thread correlation in this milestone. Entity, time, embedding, and LLM correlation remain deferred.
- Store the compact current Situation snapshot on the Situation row and protect updates with optimistic version checks.
- Add a minimal operator CLI for Goal management and read-only Situation inspection. Do not add public HTTP CRUD endpoints or a dashboard.
- Add comments for non-obvious ownership, concurrency, transition, correlation, and transaction invariants. Do not narrate self-evident code.

## Scope

Milestone 3 includes:

- Goal domain types, persistence, repository, and service
- Situation domain types, persistence, repository, and service
- Situation-to-Event and Situation-to-Goal relationships
- deterministic Situation correlation-key persistence
- Gmail-thread Situation resolution
- deterministic initial Situation title and snapshot values
- optimistic Situation snapshot updates
- Goal lifecycle transition enforcement
- Situation lifecycle transition enforcement
- stable JSON CLI output for Goal commands and Situation inspection
- Alembic migration, unit tests, PostgreSQL integration tests, and CI coverage
- operator documentation for the new CLI

Milestone 3 excludes:

- automatic consumption of every Gmail Event
- deterministic or AI relevance classification
- Signals
- goal inference
- entity extraction or semantic Situation correlation
- embeddings or pgvector retrieval
- memory and context building
- agent runtime or LLM calls
- Telegram notifications or conversations
- action proposals, approvals, policies, or tool execution
- public Goal or Situation APIs
- Situation mutation through the CLI
- Situation snapshot history or formal event-sourced projections

## Persistence Model

### Goal

`goals` stores durable user intent:

- UUID primary key
- `user_id` and `workspace_id`
- title, objective, and domain
- mode: `ACHIEVE` or `MAINTAIN`
- integer priority from 0 through 100, default 50
- status: `CANDIDATE`, `ACTIVE`, `PAUSED`, `COMPLETED`, or `ABANDONED`
- JSONB success criteria represented as a list of non-blank strings
- JSONB constraints represented as an object
- JSONB autonomy policy represented as an object
- source: `USER_EXPLICIT` or `AGENT_INFERRED`
- optional confidence from 0 through 1
- optional `parent_goal_id`
- creation and update timestamps

Title, objective, and domain are normalized and non-blank. Title is limited to 200 characters, objective to 4,000 characters, and domain to 100 characters. A Goal accepts at most 20 success criteria, each limited to 500 characters. Constraints must serialize to at most 8 KiB. An explicit Goal has confidence 1 and begins `ACTIVE`. An inferred Goal requires confidence and begins `CANDIDATE`. Milestone 3 uses the safe autonomy default:

```json
{"mode": "REQUIRE_APPROVAL"}
```

The Goal service accepts no other autonomy mode in this milestone. This field never grants permission; it is future policy input.

The table has a unique `(id, workspace_id, user_id)` key. A composite foreign key from `(workspace_id, user_id)` to the owning Workspace prevents a Goal from claiming another User's Workspace. A self-referential composite foreign key on `(parent_goal_id, workspace_id, user_id)` prevents cross-scope parent relationships.

### Situation

`situations` stores one bounded real-world case:

- UUID primary key
- `user_id` and `workspace_id`
- extensible string `situation_type`; Milestone 3 creates `EMAIL_THREAD`
- title
- lifecycle status: `OPEN`, `ACTIVE`, `WAITING_USER`, `WAITING_EXTERNAL`, `RESOLVED`, or `ABANDONED`
- attention level: `LOW`, `NORMAL`, `HIGH`, or `URGENT`
- summary
- current state
- optional next action
- optional next expected event
- positive integer snapshot version, initially 1
- creation, update, and last-activity timestamps

Title and current state are non-blank. Title is limited to 300 characters, summary to 2,000 characters, current state to 100 characters, next action to 1,000 characters, and next expected event to 1,000 characters. Summary may be empty when no safe deterministic summary exists. The table has a unique `(id, workspace_id, user_id)` key and a composite Workspace ownership foreign key.

The snapshot is the current operational view, not factual history. Raw Events remain immutable. A later milestone may add snapshot history if a concrete audit or replay requirement appears.

### SituationEvent

`situation_events` is a many-to-many relationship between Situations and immutable Events:

- `situation_id`
- `event_id`
- `workspace_id` and `user_id`
- correlation method: `DETERMINISTIC_KEY` or `EXPLICIT`
- optional correlation key
- linked timestamp

The primary key is `(situation_id, event_id)`. Composite foreign keys require both the Situation and Event to belong to the same User and Workspace. The Event table gains the redundant-but-enforcing unique key `(id, workspace_id, user_id)` required by this relationship.

### SituationGoal

`situation_goals` is a many-to-many relationship between Situations and Goals:

- `situation_id`
- `goal_id`
- `workspace_id` and `user_id`
- relevance score from 0 through 1
- contribution type: `SUPPORTS`, `BLOCKS`, or `CONTEXT`
- optional reasoning text limited to 1,000 characters
- linked timestamp

The primary key is `(situation_id, goal_id)`. Composite foreign keys prevent cross-scope links. Re-linking the same Goal is idempotent and may update relationship metadata only through an explicit service command.

### SituationCorrelationKey

`situation_correlation_keys` reserves strong provider identifiers:

- `workspace_id` and `user_id`
- correlation key
- `situation_id`
- correlation kind: `GMAIL_THREAD`
- creation timestamp

The primary key is `(workspace_id, correlation_key)`, so one strong key resolves to at most one Situation in a Workspace. A composite foreign key proves that the target Situation belongs to the same User and Workspace. Keys are bounded, non-blank, and treated as opaque identifiers after prefix validation.

## Domain Types and Lifecycle Rules

Domain commands are immutable Pydantic models. They reject blank text, naive datetimes, out-of-range numbers, duplicate identifiers, invalid JSON shapes, and inconsistent source/status combinations before persistence.

Goal transitions are:

```text
CANDIDATE -> ACTIVE | ABANDONED
ACTIVE    -> PAUSED | COMPLETED | ABANDONED
PAUSED    -> ACTIVE | COMPLETED | ABANDONED
COMPLETED -> terminal
ABANDONED -> terminal
```

Situation transitions are:

```text
OPEN             -> ACTIVE | WAITING_USER | WAITING_EXTERNAL | RESOLVED | ABANDONED
ACTIVE           -> WAITING_USER | WAITING_EXTERNAL | RESOLVED | ABANDONED
WAITING_USER     -> ACTIVE | WAITING_EXTERNAL | RESOLVED | ABANDONED
WAITING_EXTERNAL -> ACTIVE | WAITING_USER | RESOLVED | ABANDONED
RESOLVED         -> terminal
ABANDONED        -> terminal
```

An update to the same current status is idempotent. Terminal records cannot be reopened in Milestone 3.

## Goal Service

`GoalService` owns Goal behavior and delegates SQL to `GoalRepository`.

It provides operations to:

- create an explicit active Goal
- create an inferred candidate Goal for future callers and tests
- get one Goal by explicit User, Workspace, and Goal IDs
- list Goals for one Workspace with optional status filtering
- update editable fields
- transition status according to the lifecycle table

Every command carries explicit User and Workspace IDs. Ownership is never derived from free-form content or another provider. Parent Goal lookup and mutation occur in one transaction so a scope mismatch cannot create a partial record.

Goal list ordering is deterministic: priority descending, creation time ascending, then UUID ascending.

## Situation Service and Snapshot Updates

`SituationService` owns Situation behavior and delegates SQL to `SituationRepository`.

It provides operations to:

- get and list Situations within one Workspace
- link a Goal with relationship metadata
- transition lifecycle state
- change attention level
- update the current snapshot

Snapshot updates carry:

- explicit User, Workspace, and Situation IDs
- `expected_version`
- summary, current state, next action, and next expected event
- update timestamp

The repository performs one conditional update where `version = expected_version`. Success increments the version exactly once. A missing or stale row returns a typed conflict or not-found result; it never overwrites a concurrent update.

Situation list ordering is deterministic: unresolved before terminal, attention descending, last activity descending, then UUID ascending.

## Situation Resolver

`SituationResolver` is a dedicated application service. It is not registered as a general Gmail Event consumer in Milestone 3. Milestone 4 will call it only after relevance routing decides that an Event should participate in a Situation.

The resolver accepts an immutable command containing:

- Event ID
- explicit User and Workspace IDs
- zero or more Goal IDs selected by the caller
- resolution timestamp

Resolution performs one database transaction:

1. Load the Event and verify exact User and Workspace ownership.
2. Require `source=gmail` and `event_type=email.received` for Milestone 3.
3. Extract exactly one non-blank `gmail-thread:` correlation key.
4. Query the Workspace correlation registry.
5. If the key exists, load its Situation.
6. If the key does not exist, create a candidate `EMAIL_THREAD` Situation and attempt to reserve the key.
7. If a concurrent transaction won the key, delete the uncommitted candidate and use the committed winner.
8. Link the Event idempotently using `DETERMINISTIC_KEY` and the selected key.
9. Validate and link selected Goals idempotently.
10. Advance `last_activity_at` monotonically to the maximum of the existing value and Event occurrence time. Processing delay must not appear as real-world activity.
11. Return the Situation and whether the Situation, Event link, and Goal links were newly created.

The unique correlation-key constraint is the concurrency authority. Application-level conflict handling must not rely on a read-before-write check alone.

If multiple supported keys map to different Situations, the resolver returns a typed ambiguity failure without changing state. Events with no supported deterministic key are unresolvable in this milestone. Semantic fallback is intentionally absent.

### Deterministic Initial Snapshot

For the first relevant Gmail Event in a thread:

- title is `payload.headers.subject` after trimming and collapsing whitespace, bounded to 300 characters; subject-prefix interpretation is deferred
- blank title falls back to `Gmail conversation`
- summary is the bounded `payload.snippet`; invalid or absent snippets produce an empty summary
- current state is `NEW`
- lifecycle is `OPEN`
- attention is `NORMAL`
- next action and next expected event are absent
- version is 1
- last activity is the Event occurrence time

Provider text remains untrusted data. It may populate display fields but cannot alter type, ownership, lifecycle, attention, Goal links, autonomy, or permissions.

Resolving later Events updates linkage and last activity only. It does not overwrite a snapshot that a future relevance or agent layer has curated.

## Operator CLI

The CLI adds:

```text
eva goal create
eva goal list
eva goal show
eva goal update
eva situation list
eva situation show
```

All commands require explicit scope:

```text
--user-id UUID
--workspace-id UUID
```

Goal creation requires title, objective, domain, and mode. Priority defaults to 50. Success criteria may be supplied as repeated flags. Constraints are accepted as one JSON object. The CLI never accepts autonomy modes in Milestone 3; it applies the safe default.

Goal update requires a Goal ID and at least one supported change. It supports title, objective, domain, mode, priority, success criteria, constraints, parent Goal, and status. Clearing a parent is an explicit flag distinct from omitting the field.

Goal list optionally filters by status. Situation list optionally filters by lifecycle. Both list commands accept a limit from 1 through 100 and default to 50 so CLI output remains bounded. `show` commands require the domain object ID.

Successful one-shot commands print exactly one stable JSON document to stdout. Enums are strings, UUIDs and datetimes are strings, and keys are deterministic. Failures follow the existing fixed, content-free CLI error contract and return nonzero. Secret or raw email content is never printed by Goal commands; Situation inspection prints only the current snapshot and relationship identifiers, not linked Event payloads.

No Make wrappers are added because the commands are ordinary domain operations rather than long-running operator processes.

## Transactions, Idempotency, and Concurrency

- Goal creation is one transaction.
- Situation resolution, key reservation, Event linkage, Goal linkage, and activity update are one transaction.
- Duplicate Event linkage is an idempotent success.
- Duplicate Goal linkage is an idempotent success.
- Correlation-key uniqueness prevents duplicate Situations for one Gmail thread per Workspace.
- Concurrent resolution must converge on one Situation and leave no orphan candidate.
- Snapshot mutation uses optimistic versioning and never retries a stale user-supplied update automatically.
- Lifecycle transitions lock the current row before validation and update.
- Network or LLM calls are absent from all Milestone 3 database transactions.

## Error and Safety Rules

- Unknown User, Workspace, Goal, Situation, or Event identifiers return fixed typed domain failures.
- Cross-Workspace or cross-User references return scope failures and persist nothing.
- Invalid transitions persist nothing.
- Unsupported or ambiguous correlation persists nothing.
- Provider-supplied text is bounded before storage in Situation display fields.
- Relationship reasoning is bounded and never interpreted as authority.
- Errors stored or logged contain identifiers and fixed categories, not Event payloads or secrets.
- Raw Events remain immutable.
- Goal autonomy metadata cannot bypass the future policy engine.

## Migration

Alembic revision `20260831_0004_goal_situation.py` follows the Gmail connector migration.

The upgrade:

1. Adds the Event composite unique key required for scoped relationship foreign keys.
2. Creates `goals` with checks, ownership, parent-scope, and ordering indexes.
3. Creates `situations` with checks, ownership, version, and list indexes.
4. Creates `situation_correlation_keys`.
5. Creates `situation_events`.
6. Creates `situation_goals`.

The downgrade removes the relationship and correlation tables before Situations and Goals, then removes the added Event unique key. Migration tests exercise a clean upgrade and downgrade/upgrade cycle without modifying existing Event, Connector, or Gmail data.

## Module Structure

```text
src/eva_ai/
  goals/
    __init__.py
    types.py          immutable commands, records, enums, transitions
    repository.py     Goal SQL and scoped persistence
    service.py        Goal lifecycle and ownership behavior
  situations/
    __init__.py
    types.py          commands, records, relationship and result types
    repository.py     Situation, link, correlation, and snapshot SQL
    service.py        lifecycle, attention, snapshot, and query behavior
    resolver.py       deterministic Event-to-Situation resolution
  db/models/
    goals.py
    situations.py
  cli.py              command parsing, dependency composition, JSON output
```

Repositories own SQL and transaction mechanics. Services own domain rules. The resolver owns correlation orchestration. CLI functions compose services but contain no persistence rules.

## Testing Strategy

### Unit Tests

- Goal and Situation enum parsing
- command normalization and validation
- explicit Goal creation defaults
- inferred Goal candidate requirements
- Goal transition table
- Situation transition table
- safe autonomy default
- snapshot update command version validation
- Gmail title and summary derivation
- resolver rejection of unsupported, missing, duplicate, and ambiguous keys
- CLI parser and exact dispatch arguments
- stable JSON output
- fixed safe CLI failure output

### PostgreSQL Integration Tests

- migration tables, defaults, checks, indexes, and downgrade/upgrade
- Goal Workspace ownership and parent-scope constraints
- explicit and inferred Goal persistence
- Goal updates and terminal transitions
- Situation ownership and snapshot version conflicts
- monotonic last activity
- Situation-to-Goal many-to-many links
- Situation-to-Event many-to-many links
- same Gmail thread resolves to one Situation
- different Gmail threads resolve to different Situations
- duplicate resolution creates no duplicate links
- concurrent same-thread resolution converges on one Situation without orphans
- selected Goal IDs must share Event scope
- raw Event rows remain unchanged by resolution
- existing Gmail and Event regression behavior remains intact

### Full Verification

The milestone gate runs:

1. Ruff formatting check
2. Ruff lint
3. strict mypy
4. all unit and PostgreSQL integration tests
5. Alembic clean upgrade and downgrade/upgrade checks
6. CLI smoke checks
7. `git diff --check`

## Acceptance Criteria

Milestone 3 is complete when:

1. An explicit operator-created Goal becomes active and is inspectable through stable JSON CLI output.
2. An inferred Goal can be represented only as a candidate with confidence and grants no authority.
3. Goal and Situation records cannot cross User or Workspace boundaries through services or direct database writes.
4. The resolver deterministically maps one Gmail thread to one Situation and attaches later Events idempotently.
5. Different Gmail threads remain separate.
6. Concurrent first resolution for one Gmail thread leaves exactly one Situation and one correlation reservation.
7. Situation snapshots reject stale versions and preserve immutable Event history.
8. Situations support many-to-many Goal linkage with bounded metadata.
9. No background wiring creates Situations for all Gmail Events before Milestone 4 relevance exists.
10. No LLM, Telegram, memory, Signal, action, or public API behavior enters the milestone.
11. Existing Gmail ingestion, Event backbone, website, migrations, and CI remain green.
12. The branch is pushed and delivered through a pull request without merging until the user approves.
