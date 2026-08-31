# Milestone 3 Goal and Situation Domain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Apply `superpowers:test-driven-development` for every behavior change and `superpowers:verification-before-completion` before commits, push, or PR creation.

**Goal:** Add Eva's durable Goal and Situation domain, deterministic Gmail-thread correlation, and a minimal operator CLI without automatically turning every ingested email into a Situation.

**Architecture:** Keep the domain normalized and scope-safe. Pydantic domain commands/records express invariants; SQLAlchemy repositories own persistence and transactions; services own lifecycle policy; `SituationResolver` creates or reuses an email-thread Situation only when a future relevance stage explicitly invokes it. The existing Gmail ingestion path remains unchanged in Milestone 3.

**Tech Stack:** Python `>=3.14,<3.15`, Pydantic 2, SQLAlchemy 2 async, PostgreSQL 17 with pgvector, Alembic, argparse, pytest/pytest-asyncio, Ruff, strict mypy, uv.

**Spec:** `docs/superpowers/specs/2026-08-31-milestone-3-goal-situation-design.md`

## Global Constraints

- Work only on branch `codex/milestone-3-goal-situation` in `.worktrees/milestone-3-goal-situation`.
- Preserve the repository floors: Python `>=3.14,<3.15` and PostgreSQL 17.
- Follow red-green-refactor: write a focused failing test, run it and confirm the expected failure, implement the minimum behavior, then rerun the focused test.
- Preserve the existing Gmail notification, sync, normalization, event creation, and outbox paths. No ingestion component may call `SituationResolver` in this milestone.
- Every read and mutation must require both `user_id` and `workspace_id`; never fetch a Goal, Situation, Event, or relationship by object UUID alone.
- Keep all multi-table writes inside one repository transaction.
- Use safe, generic operator errors. Do not emit Gmail payloads, OAuth material, connection strings, SQL text, or internal exception details.
- Add comments where a domain invariant, concurrency technique, or intentionally deferred integration is not evident from the code itself. Avoid comments that merely restate syntax.
- Do not merge the PR. The user will review and merge it.

---

## File Responsibility Map

| File | Single responsibility |
|---|---|
| `eva_ai/goals/types.py` | Goal enums, immutable commands/records, and boundary validation |
| `eva_ai/goals/transitions.py` | Pure Goal lifecycle transition policy |
| `eva_ai/goals/repository.py` | Scoped Goal SQL and atomic persistence operations |
| `eva_ai/goals/service.py` | Explicit/inferred creation policy and Goal use cases |
| `eva_ai/situations/types.py` | Situation enums, immutable commands/records, and boundary validation |
| `eva_ai/situations/transitions.py` | Pure Situation lifecycle transition policy |
| `eva_ai/situations/repository.py` | Scoped Situation SQL, relationships, optimistic updates, and atomic resolver writes |
| `eva_ai/situations/service.py` | Situation query/edit use cases and lifecycle policy |
| `eva_ai/situations/resolver.py` | Gmail Event eligibility, deterministic-key extraction, and initial snapshot derivation |
| `eva_ai/db/models/goals.py` | Goal ORM table mapping |
| `eva_ai/db/models/situations.py` | Situation and relationship ORM table mappings |
| `eva_ai/cli.py` | Operator argument parsing, dependency composition, safe error boundary, and JSON rendering |

The existing Event and Gmail modules remain the system of record for ingestion. The resolver reads their public Event contract; it does not add Situation concerns to connector code.

## Stable Public Contracts

The following names and shapes are shared across tasks. Later tasks must import them rather than creating parallel representations.

### Goal contracts

Create `src/eva_ai/goals/types.py` with these public types:

```python
class GoalMode(StrEnum):
    ACHIEVE = "ACHIEVE"
    MAINTAIN = "MAINTAIN"


class GoalStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


class GoalSource(StrEnum):
    USER_EXPLICIT = "USER_EXPLICIT"
    AGENT_INFERRED = "AGENT_INFERRED"


class GoalDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    workspace_id: UUID
    title: str
    objective: str
    domain: str
    mode: GoalMode
    priority: int = 50
    success_criteria: tuple[str, ...] = ()
    constraints: JsonObject = Field(default_factory=dict)
    parent_goal_id: UUID | None = None


class InferredGoalDraft(GoalDraft):
    confidence: Decimal


class GoalUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    workspace_id: UUID
    goal_id: UUID
    title: str | None = None
    objective: str | None = None
    domain: str | None = None
    mode: GoalMode | None = None
    priority: int | None = None
    success_criteria: tuple[str, ...] | None = None
    constraints: JsonObject | None = None
    parent_goal_id: UUID | None = None
    clear_parent: bool = False
    status: GoalStatus | None = None


class GoalRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: UUID
    workspace_id: UUID
    title: str
    objective: str
    domain: str
    mode: GoalMode
    priority: int
    status: GoalStatus
    success_criteria: tuple[str, ...]
    constraints: JsonObject
    autonomy_policy: JsonObject
    source: GoalSource
    confidence: Decimal | None
    parent_goal_id: UUID | None
    created_at: datetime
    updated_at: datetime
```

Use a JSON type alias that accepts JSON primitives, lists, and string-keyed objects. Validate and normalize text at the command boundary:

- strip surrounding whitespace;
- reject an empty normalized string;
- `title <= 200`, `objective <= 4000`, `domain <= 100`;
- `priority` is 0 through 100;
- at most 20 success criteria, each non-empty and at most 500 characters;
- serialized constraints are at most 8 KiB;
- inferred confidence is between 0 and 1 inclusive;
- `GoalUpdate` must contain at least one mutable field or `clear_parent=True`;
- `parent_goal_id` and `clear_parent=True` are mutually exclusive.

The only Milestone 3 autonomy policy is the immutable value `{"mode": "REQUIRE_APPROVAL"}`. Return a new mapping when constructing each record so callers cannot share mutable state.

### Situation contracts

Create `src/eva_ai/situations/types.py` with these public types:

```python
class SituationType(StrEnum):
    EMAIL_THREAD = "EMAIL_THREAD"


class SituationLifecycle(StrEnum):
    OPEN = "OPEN"
    ACTIVE = "ACTIVE"
    WAITING_USER = "WAITING_USER"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    RESOLVED = "RESOLVED"
    ABANDONED = "ABANDONED"


class AttentionLevel(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class CorrelationMethod(StrEnum):
    DETERMINISTIC_KEY = "DETERMINISTIC_KEY"
    EXPLICIT = "EXPLICIT"


class CorrelationKeyKind(StrEnum):
    GMAIL_THREAD = "GMAIL_THREAD"


class GoalContribution(StrEnum):
    SUPPORTS = "SUPPORTS"
    BLOCKS = "BLOCKS"
    CONTEXT = "CONTEXT"


class SituationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: UUID
    workspace_id: UUID
    type: SituationType
    title: str
    lifecycle: SituationLifecycle
    attention: AttentionLevel
    summary: str
    current_state: str
    next_action: str | None
    next_expected: str | None
    version: int
    last_activity_at: datetime
    created_at: datetime
    updated_at: datetime


class SituationSnapshotUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    workspace_id: UUID
    situation_id: UUID
    expected_version: int
    title: str
    summary: str
    current_state: str
    next_action: str | None = None
    next_expected: str | None = None
    updated_at: datetime


class LinkSituationGoal(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    workspace_id: UUID
    situation_id: UUID
    goal_id: UUID
    relevance: Decimal
    contribution: GoalContribution
    reasoning: str | None = None
    linked_at: datetime


class ResolveEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: UUID
    user_id: UUID
    workspace_id: UUID
    goal_ids: tuple[UUID, ...] = ()
    resolved_at: datetime


class SituationResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    situation: SituationRecord
    situation_created: bool
    event_link_created: bool
    linked_goal_ids: tuple[UUID, ...]
```

`SituationResolution.linked_goal_ids` contains only Goal links newly inserted by that resolver call, sorted by UUID. Existing links are intentionally absent so callers can distinguish idempotent reuse.

Also define `SituationGoalRecord` and `SituationEventRecord` with their scoped identifiers, relationship attributes, and timestamps. Define the shared JSON aliases before the commands:

```python
JsonObject = dict[str, JsonValue]
```

Validate:

- `title <= 300`, `summary <= 2000`, `current_state <= 100`;
- `next_action` and `next_expected` are nullable and at most 1000 characters;
- `expected_version >= 1`;
- goal relevance is between 0 and 1 inclusive;
- relationship reasoning, when provided, is normalized, non-empty, and at most 1000 characters;
- all command timestamps are timezone-aware.

### Error contracts

Create `src/eva_ai/goals/errors.py` and `src/eva_ai/situations/errors.py`. Expose domain-specific exception classes, all with safe constant messages:

```python
class GoalError(Exception):
    """Base class for safe Goal-domain failures."""

class GoalNotFoundError(GoalError):
    """The requested Goal is unavailable in the supplied scope."""

class GoalScopeError(GoalError):
    """The Goal operation has an invalid user/workspace scope."""

class InvalidGoalTransitionError(GoalError):
    """The requested Goal lifecycle transition is forbidden."""

class GoalParentError(GoalError):
    """The requested parent Goal is invalid for this scope."""

class SituationError(Exception):
    """Base class for safe Situation-domain failures."""

class SituationNotFoundError(SituationError):
    """The requested Situation is unavailable in the supplied scope."""

class SituationScopeError(SituationError):
    """The Situation operation has an invalid user/workspace scope."""

class InvalidSituationTransitionError(SituationError):
    """The requested Situation lifecycle transition is forbidden."""

class SituationVersionConflictError(SituationError):
    """The Situation snapshot changed after the caller read it."""

class SituationResolutionError(SituationError):
    """The Event cannot be resolved into a Situation safely."""
```

Do not include UUIDs, database details, or source payload contents in exception messages.

## Task 1: Domain Types, Validation, and Lifecycle Rules

**Files:**

- Create: `src/eva_ai/goals/__init__.py`
- Create: `src/eva_ai/goals/errors.py`
- Create: `src/eva_ai/goals/types.py`
- Create: `src/eva_ai/goals/transitions.py`
- Create: `src/eva_ai/situations/__init__.py`
- Create: `src/eva_ai/situations/errors.py`
- Create: `src/eva_ai/situations/types.py`
- Create: `src/eva_ai/situations/transitions.py`
- Create: `tests/unit/goals/__init__.py`
- Create: `tests/unit/goals/test_types.py`
- Create: `tests/unit/goals/test_transitions.py`
- Create: `tests/unit/situations/__init__.py`
- Create: `tests/unit/situations/test_types.py`
- Create: `tests/unit/situations/test_transitions.py`

**Interfaces:**

- Consumes: Pydantic 2 `BaseModel`, `ConfigDict`, field/model validators; standard `StrEnum`, `UUID`, `Decimal`, and timezone-aware `datetime`.
- Produces: every Goal/Situation command, record, enum, error, and transition function listed in **Stable Public Contracts**. Tasks 2 through 6 import these names unchanged.

- [ ] **Step 1: Write failing Goal validation tests**

Cover whitespace normalization, every length/count/range boundary, the 8 KiB constraint limit, update field presence, parent clearing exclusivity, inferred confidence, frozen records, and fresh safe-autonomy mappings.

Run:

```bash
uv run pytest tests/unit/goals/test_types.py -q
```

Expected: collection fails because `eva_ai.goals` does not exist.

- [ ] **Step 2: Implement Goal types and errors**

Implement the stable contracts above with Pydantic field/model validators. Use compact JSON serialization with UTF-8 byte length for the constraint cap:

```python
len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
```

Export the public errors, enums, commands, and records from `eva_ai.goals`.

Run:

```bash
uv run pytest tests/unit/goals/test_types.py -q
```

Expected: all Goal type tests pass.

- [ ] **Step 3: Write failing Goal transition tests**

Exercise the complete matrix from the design spec, including same-state idempotency and terminal-state rejection. Test a pure function:

```python
def validate_goal_transition(current: GoalStatus, requested: GoalStatus) -> None:
    """Raise InvalidGoalTransitionError unless the transition is allowed."""
```

Run:

```bash
uv run pytest tests/unit/goals/test_transitions.py -q
```

Expected: import or assertion failure before implementation.

- [ ] **Step 4: Implement Goal transitions**

Encode the transition table explicitly as immutable sets. Same-state requests return normally. Add a comment explaining why terminal states have no outward transitions.

- [ ] **Step 5: Write failing Situation validation and transition tests**

Cover all boundaries in the stable contract, timezone-aware timestamps, uniqueness normalization of `goal_ids`, the full lifecycle matrix, and same-state idempotency.

Run:

```bash
uv run pytest tests/unit/situations/test_types.py tests/unit/situations/test_transitions.py -q
```

Expected: imports or assertions fail before implementation.

- [ ] **Step 6: Implement Situation types, errors, and transitions**

Expose:

```python
def validate_situation_transition(
    current: SituationLifecycle,
    requested: SituationLifecycle,
) -> None:
    """Raise InvalidSituationTransitionError unless the transition is allowed."""
```

Export the public contracts from `eva_ai.situations`.

- [ ] **Step 7: Verify and commit Task 1**

Run:

```bash
uv run pytest tests/unit/goals tests/unit/situations -q
uv run ruff check src/eva_ai/goals src/eva_ai/situations tests/unit/goals tests/unit/situations
uv run mypy src/eva_ai/goals src/eva_ai/situations
```

Expected: all commands pass.

Commit:

```bash
git add src/eva_ai/goals src/eva_ai/situations tests/unit/goals tests/unit/situations
git commit -m "feat: define goal and situation domain contracts"
```

## Task 2: Relational Schema, ORM Models, and Migration

**Files:**

- Create: `src/eva_ai/db/models/goals.py`
- Create: `src/eva_ai/db/models/situations.py`
- Modify: `src/eva_ai/db/models/events.py`
- Modify: `src/eva_ai/db/models/__init__.py`
- Create: `migrations/versions/20260831_0004_goal_situation.py`
- Modify: `tests/integration/test_migrations.py`
- Create: `tests/integration/goals/__init__.py`
- Create: `tests/integration/goals/test_schema.py`
- Create: `tests/integration/situations/__init__.py`
- Create: `tests/integration/situations/test_schema.py`

**Interfaces:**

- Consumes: Task 1 enums; existing `Base`, `UUIDPrimaryKeyMixin`, `TimestampMixin`, `User`, `Workspace`, and `Event` ORM mappings.
- Produces: exported ORM classes `Goal`, `Situation`, `SituationEvent`, `SituationGoal`, and `SituationCorrelationKey`, plus Event scoped uniqueness and Alembic revision `20260831_0004` for repositories in Tasks 3 through 5.

- [ ] **Step 1: Write failing schema tests**

Assert tables, columns, defaults, checks, indexes, foreign keys, uniqueness, and delete behavior. Store enum values as bounded strings with named checks, matching the existing Event models. Required named checks include:

```text
ck_goals_mode
ck_goals_status
ck_goals_source
ck_situations_type
ck_situations_lifecycle
ck_situations_attention
ck_situation_events_method
ck_situation_correlation_keys_kind
ck_situation_goals_contribution
```

Required tables and principal constraints:

- `goals`: composite FK `(workspace_id, user_id)` to workspaces; scoped unique `(id, workspace_id, user_id)`; scoped parent FK `(parent_goal_id, workspace_id, user_id)` to goals; checks for priority, confidence, JSONB success-criteria array shape/cardinality, and JSONB object shapes; indexes on scope/status and scope/priority.
- `situations`: composite FK `(workspace_id, user_id)`; scoped unique `(id, workspace_id, user_id)`; version `>= 1`; indexes on scope/lifecycle and scope/last activity.
- `situation_events`: PK `(situation_id, event_id)` plus stored `workspace_id` and `user_id`; composite FKs to both situations and events. Do not add Event-only uniqueness: the approved relationship is many-to-many.
- `situation_goals`: PK `(situation_id, goal_id)` plus scope; composite FKs to situations and goals; relevance check 0..1.
- `situation_correlation_keys`: PK `(workspace_id, correlation_key)`; includes `user_id`, `situation_id`, and key kind; composite FK to Situation and workspace; index on scoped Situation lookup.
- `events`: add unique `(id, workspace_id, user_id)` to support a fully scoped relationship FK.

Run:

```bash
uv run pytest tests/integration/goals/test_schema.py tests/integration/situations/test_schema.py -q
```

Expected: failures because the new tables do not exist.

- [ ] **Step 2: Implement ORM models**

Use SQLAlchemy typed mappings and the existing UUID/timestamp mixins. Store:

- success criteria, constraints, and autonomy policy as `JSONB`;
- confidence/relevance as `NUMERIC(4, 3)`;
- all timestamps as timezone-aware;
- enum values through bounded `String` columns with named `CheckConstraint` values, matching `events.py`.

Use database and Pydantic field names consistently. Do not put domain transition logic in ORM models.

- [ ] **Step 3: Implement the Alembic migration**

Set:

```python
revision = "20260831_0004"
down_revision = "20260830_0003"
```

Create parent tables before relationships and add the Event scoped unique before the Situation-Event FK. Downgrade in exact reverse dependency order. This migration creates no PostgreSQL enum types.

- [ ] **Step 4: Add migration round-trip coverage**

Extend `tests/integration/test_migrations.py` to prove:

1. upgrade from `20260830_0003` to `20260831_0004` succeeds;
2. all five tables and Event uniqueness exist;
3. an Event inserted before upgrade retains the same scalar, JSONB, and array values after upgrade;
4. downgrade to `20260830_0003` removes only Milestone 3 schema;
5. re-upgrade succeeds.

Run:

```bash
uv run pytest tests/integration/test_migrations.py tests/integration/goals/test_schema.py tests/integration/situations/test_schema.py -q
```

Expected: all schema and migration tests pass.

- [ ] **Step 5: Verify model registration and commit Task 2**

Run:

```bash
uv run python -c "from eva_ai.db.models import Goal, Situation, SituationCorrelationKey, SituationEvent, SituationGoal; print(Goal.__tablename__, Situation.__tablename__)"
uv run ruff check src/eva_ai/db/models migrations/versions/20260831_0004_goal_situation.py tests/integration
uv run mypy src/eva_ai/db/models
```

Expected: imports print `goals situations`; static checks pass.

Commit:

```bash
git add src/eva_ai/db/models migrations/versions/20260831_0004_goal_situation.py tests/integration/test_migrations.py tests/integration/goals tests/integration/situations
git commit -m "feat: add goal and situation persistence schema"
```

## Task 3: Goal Repository and Service

**Files:**

- Create: `src/eva_ai/goals/repository.py`
- Create: `src/eva_ai/goals/service.py`
- Modify: `src/eva_ai/goals/__init__.py`
- Create: `tests/integration/goals/test_repository.py`
- Create: `tests/integration/goals/test_service.py`

**Interfaces:**

- Consumes: Task 1 Goal commands/records/errors/transitions; Task 2 `Goal` mapping; existing `Database` transaction/session API.
- Produces: `GoalRepository` and `GoalService` with the exact methods below. Task 5 uses scoped Goal rows during resolver linking; Task 6 composes `GoalService` for operator commands.

### Required interfaces

```python
class GoalRepository:
    def __init__(self, database: Database) -> None:
        """Store the database dependency used by scoped transactions."""

    async def create(
        self,
        draft: GoalDraft,
        *,
        source: GoalSource,
        status: GoalStatus,
        confidence: Decimal,
    ) -> GoalRecord:
        """Persist one Goal after validating workspace and parent scope."""

    async def get(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        goal_id: UUID,
    ) -> GoalRecord | None:
        """Return a Goal only when all three scoped identifiers match."""

    async def list(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        statuses: tuple[GoalStatus, ...] = (),
        limit: int = 50,
    ) -> tuple[GoalRecord, ...]:
        """Return a bounded, deterministically ordered scoped Goal list."""

    async def update(self, command: GoalUpdate) -> GoalRecord:
        """Atomically validate and apply the requested Goal mutation."""


class GoalService:
    def __init__(self, repository: GoalRepository) -> None:
        """Store the repository used by Goal use cases."""

    async def create_explicit(self, draft: GoalDraft) -> GoalRecord:
        """Create an ACTIVE, USER_EXPLICIT Goal with confidence 1."""

    async def create_inferred(self, draft: InferredGoalDraft) -> GoalRecord:
        """Create a CANDIDATE, AGENT_INFERRED Goal."""

    async def get(self, *, user_id: UUID, workspace_id: UUID, goal_id: UUID) -> GoalRecord:
        """Get one scoped Goal or raise GoalNotFoundError."""

    async def list(self, *, user_id: UUID, workspace_id: UUID, statuses: tuple[GoalStatus, ...] = (), limit: int = 50) -> tuple[GoalRecord, ...]:
        """List Goals after validating the requested limit."""

    async def update(self, command: GoalUpdate) -> GoalRecord:
        """Apply one validated Goal mutation."""
```

- [ ] **Step 1: Write failing repository tests for creation and scoped reads**

Cover explicit and inferred persisted shapes, exact safe autonomy policy, parent relationship within scope, wrong-user and wrong-workspace reads, missing parent rejection, cross-scope parent rejection, and transaction rollback after invalid parent.

Use `tests/integration/factories.py:create_scope`; add only a small `create_goal` factory if repeated setup materially obscures assertions.

Run:

```bash
uv run pytest tests/integration/goals/test_repository.py -q
```

Expected: import failure before repository implementation.

- [ ] **Step 2: Implement create/get mapping**

The repository must validate the composite workspace scope and parent scope within the same create transaction. Convert ORM rows to `GoalRecord` in one private mapper. Integrity errors become safe domain errors, never raw SQLAlchemy exceptions.

- [ ] **Step 3: Write failing list-order tests**

Assert filters and exact ordering:

1. priority descending;
2. creation ascending;
3. UUID ascending.

Test limits 1 and 100 and reject values outside that range at the service boundary.

- [ ] **Step 4: Implement list behavior**

Use one bounded query; do not load all Goals and sort in Python.

- [ ] **Step 5: Write failing update and lifecycle tests**

Cover each mutable field, parent set/clear, same-state idempotency, every allowed transition, terminal transition rejection, wrong scope, missing Goal, and rollback when a mixed update contains an invalid transition or parent.

- [ ] **Step 6: Implement atomic updates**

Lock the scoped target Goal row with `SELECT FOR UPDATE` and validate status/parent before applying any field. A same-status request may still update other supplied fields. Preserve immutable source, confidence, autonomy policy, scope, and creation timestamp.

- [ ] **Step 7: Implement the service policy and service tests**

The service sets:

- explicit: `source=USER_EXPLICIT`, `status=ACTIVE`, `confidence=Decimal("1")`;
- inferred: `source=AGENT_INFERRED`, `status=CANDIDATE`, caller-provided confidence.

It enforces list limits, translates missing repository records into `GoalNotFoundError`, and delegates transactional mutations. Confirm Goal autonomy does not invoke tools or approve any action.

- [ ] **Step 8: Verify and commit Task 3**

Run:

```bash
uv run pytest tests/unit/goals tests/integration/goals -q
uv run ruff check src/eva_ai/goals tests/unit/goals tests/integration/goals
uv run mypy src/eva_ai/goals
```

Expected: all commands pass.

Commit:

```bash
git add src/eva_ai/goals tests/unit/goals tests/integration/goals
git commit -m "feat: add scoped goal service"
```

## Task 4: Situation Repository and Service

**Files:**

- Create: `src/eva_ai/situations/repository.py`
- Create: `src/eva_ai/situations/service.py`
- Modify: `src/eva_ai/situations/__init__.py`
- Create: `tests/integration/situations/test_repository.py`
- Create: `tests/integration/situations/test_service.py`

**Interfaces:**

- Consumes: Task 1 Situation commands/records/errors/transitions; Task 2 Situation relationship mappings; existing `Database` API.
- Produces: `SituationRepository` query/update methods and `SituationService`. Task 5 extends the same repository with resolver writes; Task 6 composes the service for operator reads.

### Required interfaces

```python
class SituationRepository:
    def __init__(self, database: Database) -> None:
        """Store the database dependency used by scoped transactions."""

    async def get(self, *, user_id: UUID, workspace_id: UUID, situation_id: UUID) -> SituationRecord | None:
        """Return a Situation only when every scoped identifier matches."""

    async def list(self, *, user_id: UUID, workspace_id: UUID, lifecycles: tuple[SituationLifecycle, ...] = (), limit: int = 50) -> tuple[SituationRecord, ...]:
        """Return a bounded, deterministically ordered Situation list."""

    async def update_snapshot(self, command: SituationSnapshotUpdate) -> SituationRecord:
        """Conditionally update a snapshot at its expected version."""

    async def update_lifecycle(self, *, user_id: UUID, workspace_id: UUID, situation_id: UUID, lifecycle: SituationLifecycle) -> SituationRecord:
        """Atomically apply a validated lifecycle transition."""

    async def update_attention(self, *, user_id: UUID, workspace_id: UUID, situation_id: UUID, attention: AttentionLevel) -> SituationRecord:
        """Update attention without changing snapshot version."""

    async def link_goal(self, command: LinkSituationGoal) -> SituationGoalRecord:
        """Create or explicitly update one same-scope Goal relationship."""

    async def list_events(self, *, user_id: UUID, workspace_id: UUID, situation_id: UUID) -> tuple[SituationEventRecord, ...]:
        """Return safe Event relationship metadata without payloads."""

    async def list_goals(self, *, user_id: UUID, workspace_id: UUID, situation_id: UUID) -> tuple[SituationGoalRecord, ...]:
        """Return Goal relationship metadata for a scoped Situation."""


class SituationService:
    def __init__(self, repository: SituationRepository) -> None:
        """Store the repository used by Situation use cases."""

    async def get(self, *, user_id: UUID, workspace_id: UUID, situation_id: UUID) -> SituationRecord:
        """Get one scoped Situation or raise SituationNotFoundError."""

    async def list(self, *, user_id: UUID, workspace_id: UUID, lifecycles: tuple[SituationLifecycle, ...] = (), limit: int = 50) -> tuple[SituationRecord, ...]:
        """List Situations after validating the requested limit."""

    async def update_snapshot(self, command: SituationSnapshotUpdate) -> SituationRecord:
        """Apply a curated snapshot mutation with optimistic locking."""

    async def update_lifecycle(self, *, user_id: UUID, workspace_id: UUID, situation_id: UUID, lifecycle: SituationLifecycle) -> SituationRecord:
        """Validate and apply a lifecycle mutation."""

    async def update_attention(self, *, user_id: UUID, workspace_id: UUID, situation_id: UUID, attention: AttentionLevel) -> SituationRecord:
        """Apply an attention mutation."""

    async def link_goal(self, command: LinkSituationGoal) -> SituationGoalRecord:
        """Validate and create an explicit Situation-to-Goal relationship."""
```

- [ ] **Step 1: Write failing scoped read and ordering tests**

Seed Situation rows directly through test helpers until the resolver exists. Cover wrong-user/workspace isolation and exact ordering:

1. unresolved before `RESOLVED` and `ABANDONED`;
2. attention `URGENT`, `HIGH`, `NORMAL`, `LOW`;
3. `last_activity_at` descending;
4. UUID ascending.

Run:

```bash
uv run pytest tests/integration/situations/test_repository.py -q
```

Expected: import failure before implementation.

- [ ] **Step 2: Implement scoped get/list and relationship projections**

Use SQL ordering expressions, bounded queries, and record mappers. `list_events` may expose Event IDs, occurrence timestamps, methods, and correlation keys but never Event payloads. `list_goals` exposes relationship metadata only.

- [ ] **Step 3: Write failing optimistic snapshot tests**

Cover:

- a successful update where `expected_version` matches;
- exactly one version increment;
- stale expected version rejected with `SituationVersionConflictError` and no partial mutation;
- wrong scope and missing Situation;
- lifecycle/attention are unchanged by snapshot editing.

- [ ] **Step 4: Implement conditional snapshot update**

Use one scoped conditional update:

```sql
UPDATE situations
SET title = :title,
    summary = :summary,
    current_state = :current_state,
    next_action = :next_action,
    next_expected = :next_expected,
    version = version + 1,
    updated_at = :updated_at
WHERE id = :id
  AND workspace_id = :workspace_id
  AND user_id = :user_id
  AND version = :expected_version
RETURNING id, user_id, workspace_id, type, title, lifecycle, attention,
          summary, current_state, next_action, next_expected, version,
          last_activity_at, created_at, updated_at
```

If it returns no row, perform one safe scoped existence check to distinguish not-found from version conflict.

- [ ] **Step 5: Write failing lifecycle, attention, and Goal-link tests**

Exercise the transition matrix, same-state idempotency, terminal rejection, attention changes, immutable fields, and scope isolation. For `link_goal`, assert same-scope creation, explicit metadata replacement on a repeated service command, nullable/normalized reasoning, bounded relevance, and complete rollback for missing or cross-scope Goals/Situations.

- [ ] **Step 6: Implement lifecycle, attention, and Goal-link mutations**

Lock the target row for lifecycle transitions. Attention updates do not increment snapshot version; lifecycle updates do not increment snapshot version. This keeps version semantics limited to curated snapshot edits. `link_goal` verifies both composite scopes and uses an upsert on `(situation_id, goal_id)` inside one transaction. Because this is the explicit relationship-mutation service, a repeat replaces relevance, contribution, reasoning, and `linked_at`. Resolver-internal conflict-safe linking in Task 5 remains insert-only and never overwrites this curated metadata.

- [ ] **Step 7: Implement service policy and verify**

The service converts missing rows to `SituationNotFoundError`, validates limit 1..100, owns lifecycle validation, and exposes explicit Goal linking with caller-supplied relationship metadata. It does not create Situations; creation remains resolver-only.

Run:

```bash
uv run pytest tests/unit/situations tests/integration/situations/test_repository.py tests/integration/situations/test_service.py -q
uv run ruff check src/eva_ai/situations tests/unit/situations tests/integration/situations
uv run mypy src/eva_ai/situations
```

Expected: all commands pass.

- [ ] **Step 8: Commit Task 4**

```bash
git add src/eva_ai/situations tests/unit/situations tests/integration/situations
git commit -m "feat: add scoped situation service"
```

## Task 5: Deterministic Gmail Situation Resolver

**Files:**

- Create: `src/eva_ai/situations/resolver.py`
- Modify: `src/eva_ai/situations/repository.py`
- Modify: `src/eva_ai/situations/__init__.py`
- Create: `tests/unit/situations/test_resolver.py`
- Create: `tests/integration/situations/test_resolver.py`

**Interfaces:**

- Consumes: existing immutable Event rows, Task 1 `ResolveEvent`/`SituationResolution`, Task 2 Event/Situation relationship mappings, Task 3 scoped Goals, and Task 4 `SituationRepository`.
- Produces: `SituationResolver.resolve(command: ResolveEvent) -> SituationResolution` and the repository's two resolution methods below. Milestone 4 will be the first runtime caller.

### Required interfaces

```python
class SituationResolver:
    def __init__(self, repository: SituationRepository) -> None:
        """Store the repository that performs atomic resolution writes."""

    async def resolve(self, command: ResolveEvent) -> SituationResolution:
        """Resolve one eligible Gmail Event under its explicit scope."""


class SituationRepository:
    async def get_event_for_resolution(
        self,
        *,
        event_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
    ) -> ResolvableEvent | None:
        """Load immutable Event data needed for resolver validation."""

    async def resolve_gmail_event(
        self,
        *,
        command: ResolveEvent,
        correlation_key: str,
        initial_snapshot: InitialSituationSnapshot,
    ) -> SituationResolution:
        """Atomically reserve/reuse a correlation key and create links."""
```

`ResolvableEvent` is an internal frozen Pydantic projection defined in `resolver.py` with exactly `id`, `user_id`, `workspace_id`, `source`, `event_type`, `occurred_at`, `payload`, and `correlation_keys`. `InitialSituationSnapshot` is an internal frozen Pydantic value with title, summary, state, type, lifecycle, attention, next fields, and last activity. Neither is exported as an operator API.

- [ ] **Step 1: Write failing pure resolver tests**

Use a fake repository to prove precondition behavior:

- Event must exist in the explicit scope;
- source must be `gmail`;
- type must be `email.received`;
- exactly one correlation key must start with `gmail-thread:`;
- duplicate `goal_ids` normalize to deterministic UUID order;
- malformed payload fields result in safe fallbacks, not payload leakage.

Test initial snapshot derivation:

- subject is stripped and internal whitespace collapsed, then capped at 300;
- no `Re:`, `Fwd:`, or similar semantic prefix stripping;
- missing/blank subject becomes `Gmail conversation`;
- summary is the normalized snippet capped at 2000;
- `current_state="NEW"`, `OPEN`, `NORMAL`, null next fields, version 1;
- `last_activity_at=event.occurred_at`, never `resolved_at`.

Run:

```bash
uv run pytest tests/unit/situations/test_resolver.py -q
```

Expected: import failure before resolver implementation.

- [ ] **Step 2: Implement resolver validation and snapshot derivation**

Keep payload parsing in small private functions that accept unknown JSON safely. Explain in a comment that resolution is intentionally opt-in until Milestone 4 supplies relevance decisions.

- [ ] **Step 3: Write failing first-event integration tests**

Create a normalized Gmail Event through the existing Event service and call the resolver. Assert one transaction creates:

- one `EMAIL_THREAD` Situation;
- one `GMAIL_THREAD` correlation-key reservation;
- one deterministic event link;
- requested goal links only after every Goal is verified in the same scope;
- initial snapshot and timestamps exactly as specified.

Also assert an invalid/missing/cross-scope Goal rolls back the Situation, correlation key, event link, and every goal link.

- [ ] **Step 4: Implement the atomic first-event path**

Within one transaction:

1. re-read and verify the Event under `(id, workspace_id, user_id)`;
2. verify all goal IDs in scope before writing relationships;
3. look up `(workspace_id, correlation_key)`;
4. if absent, insert the Situation and correlation key;
5. insert the scoped event relationship;
6. insert goal relationships with `contribution=CONTEXT`, `relevance=1.000`, and reasoning `Explicitly linked during situation resolution`;
7. return flags based on actual inserts.

The `resolved_at` field is audit input for deterministic command handling; it must not replace Event occurrence time as activity.

- [ ] **Step 5: Write failing repeat and later-event tests**

Cover:

- resolving the same Event twice is idempotent;
- a second Event with the same Gmail thread reuses the Situation;
- a second Event with a different Gmail thread in the same Workspace creates a distinct Situation;
- later Event creates one new event link and no new correlation key;
- later Event never overwrites title, summary, current state, next fields, attention, lifecycle, or version;
- activity becomes `max(existing last_activity_at, event occurred_at)` so out-of-order processing never moves time backwards;
- repeated resolver Goal links are idempotent and do not overwrite metadata created by `SituationService.link_goal`;
- a Gmail Event from another workspace with the same thread ID gets a distinct Situation.
- raw Event columns and JSON values are byte-for-byte unchanged after every resolution path;
- `linked_goal_ids` reports only links inserted by that call in UUID order.

- [ ] **Step 6: Implement the reuse path**

Use PostgreSQL conflict-safe inserts for event/goal links. Always re-check the correlation row's user and target scope before reuse. Never trust only the workspace-key primary key after lookup.

- [ ] **Step 7: Write and pass a concurrency test**

Start two independent async transactions resolving different Events from the same Gmail thread at the same time. Synchronize task release with an asyncio barrier/event. Assert both results reference one Situation and the database contains:

- one correlation key;
- one Situation;
- two event links;
- no orphan transient Situation.

Implement winner/loser handling with this correlation reservation shape:

```sql
INSERT INTO situation_correlation_keys (
    workspace_id, correlation_key, user_id, situation_id, kind, created_at
)
VALUES (
    :workspace_id, :correlation_key, :user_id, :situation_id,
    'GMAIL_THREAD', :resolved_at
)
ON CONFLICT (workspace_id, correlation_key) DO NOTHING
RETURNING situation_id
```

If the current transaction loses, delete its unreferenced transient Situation, load the winner, and continue linking. Add a focused comment because this concurrency invariant is not obvious.

- [ ] **Step 8: Prove Gmail ingestion remains decoupled**

Add a regression assertion to the resolver integration suite or existing Gmail ingestion suite: ingesting an email Event without calling `SituationResolver` leaves all Situation tables empty.

- [ ] **Step 9: Verify and commit Task 5**

Run:

```bash
uv run pytest tests/unit/situations/test_resolver.py tests/integration/situations/test_resolver.py tests/integration/connectors/test_gmail_ingestion.py -q
uv run ruff check src/eva_ai/situations tests/unit/situations tests/integration/situations
uv run mypy src/eva_ai/situations
```

Expected: all commands pass, including the concurrency case.

Commit:

```bash
git add src/eva_ai/situations tests/unit/situations tests/integration/situations tests/integration/connectors/test_gmail_ingestion.py
git commit -m "feat: resolve relevant gmail threads into situations"
```

## Task 6: Operator CLI for Goals and Situations

**Files:**

- Modify: `src/eva_ai/cli.py`
- Modify: `tests/unit/test_cli.py`
- Create: `tests/integration/test_goal_situation_cli.py`

**Interfaces:**

- Consumes: Task 3 `GoalService`; Task 4 `SituationService`; existing `Database`, logging, `CommandFunctions`, parser, and dispatch seams in `eva_ai.cli`.
- Produces: the six operator commands and stable JSON/error contracts below. It does not expose the resolver as an operator command.

### Command surface

```text
eva goal create --user-id UUID --workspace-id UUID --title TEXT --objective TEXT --domain TEXT --mode {ACHIEVE,MAINTAIN} [--priority 0..100] [repeatable --success-criterion TEXT] [--constraints-json JSON] [--parent-goal-id UUID]
eva goal list --user-id UUID --workspace-id UUID [repeatable --status STATUS] [--limit 1..100]
eva goal show --user-id UUID --workspace-id UUID --goal-id UUID
eva goal update --user-id UUID --workspace-id UUID --goal-id UUID [mutable fields]
eva situation list --user-id UUID --workspace-id UUID [repeatable --lifecycle LIFECYCLE] [--limit 1..100]
eva situation show --user-id UUID --workspace-id UUID --situation-id UUID
```

`goal update` supports `--title`, `--objective`, `--domain`, `--mode`, `--priority`, repeated `--success-criterion`, `--constraints-json`, `--parent-goal-id`, `--clear-parent`, and `--status`. The domain service accepts an empty success-criteria tuple; the Milestone 3 CLI only replaces criteria when at least one repeated flag is present.

- [ ] **Step 1: Write failing parser and dispatch tests**

Extend `CommandFunctions` with exactly six callables:

```python
goal_create
goal_list
goal_show
goal_update
situation_list
situation_show
```

Test successful parsing, enum choices, repeated filters/criteria, defaults, invalid UUID/JSON/limit values, parent option exclusivity, and update-with-no-fields rejection. Existing scope and Gmail command tests must remain unchanged and passing.

Run:

```bash
uv run pytest tests/unit/test_cli.py -q
```

Expected: new cases fail before parser changes.

- [ ] **Step 2: Implement parser and command dispatch**

Create the database, repository, and service only for the selected command. Preserve the existing dependency-injection seam used by unit tests. Parse constraints using a helper that requires a JSON object, not a list or primitive.

- [ ] **Step 3: Write failing JSON contract tests**

All successes print exactly one JSON document followed by a newline:

- create/show/update: one full Goal record;
- goal list: an object with `items: list[GoalRecord]` and integer `count`;
- situation list: an object with `items: list[SituationRecord]` and integer `count`;
- situation show: an object with `situation: SituationRecord`, `event_links: list[SituationEventRecord]`, and `goal_links: list[SituationGoalRecord]`.

Use canonical strings for UUIDs, enum values, decimals, and ISO-8601 UTC timestamps. Situation output contains relationship metadata but no Event payload, Gmail body, headers, snippets, or connector secrets.

Errors write one short generic line to stderr and exit non-zero. Unit tests must confirm an injected database exception does not echo its original message.

- [ ] **Step 4: Implement serialization and safe error boundary**

Use one recursive serializer for Pydantic models, tuples, UUID, Decimal, datetime, and StrEnum values. Keep the final operator error mapping centralized in `cli.py`; log sanitized exception class/context through the existing logging system without exposing it to stdout/stderr.

- [ ] **Step 5: Add CLI integration tests**

Exercise real services and PostgreSQL for:

1. explicit Goal create -> ACTIVE/USER_EXPLICIT/confidence 1;
2. list ordering/filter;
3. show;
4. update and status transition;
5. Situation list/show after resolving a relevant Gmail Event;
6. wrong scope returns a safe not-found error;
7. Situation show omits raw Event payload content.

- [ ] **Step 6: Verify and commit Task 6**

Run:

```bash
uv run pytest tests/unit/test_cli.py tests/integration/test_goal_situation_cli.py -q
uv run ruff check src/eva_ai/cli.py tests/unit/test_cli.py tests/integration/test_goal_situation_cli.py
uv run mypy src/eva_ai/cli.py
```

Expected: all commands pass.

Commit:

```bash
git add src/eva_ai/cli.py tests/unit/test_cli.py tests/integration/test_goal_situation_cli.py
git commit -m "feat: add goal and situation operator commands"
```

## Task 7: Operator Documentation and Milestone Verification

**Files:**

- Create: `docs/goal-situation-operator.md`
- Modify: `README.md`

**Interfaces:**

- Consumes: the finished behavior and command syntax from Tasks 1 through 6, plus the approved design specification.
- Produces: operator-facing usage/troubleshooting documentation, verified source/tests, a pushed feature branch, and an open unmerged PR.

- [ ] **Step 1: Write operator documentation**

Document:

- the plain-language relationship between Events, Goals, and Situations;
- explicit versus inferred Goal semantics;
- every CLI command with valid, copyable examples;
- the fixed approval-required autonomy policy;
- deterministic Gmail-thread correlation;
- the important Milestone 3 boundary: ingestion alone does not create Situations;
- that Milestone 4 will invoke resolution after relevance evaluation;
- lifecycle transition tables and optimistic snapshot version behavior;
- safe troubleshooting steps for scope mismatch, invalid transitions, stale versions, and malformed JSON.

Update the README architecture/status section to link to this guide and state Milestone 3 capability without claiming Milestone 4 routing exists.

- [ ] **Step 2: Run focused domain verification**

```bash
uv run pytest tests/unit/goals tests/unit/situations tests/integration/goals tests/integration/situations tests/integration/test_goal_situation_cli.py -q
```

Expected: all focused Milestone 3 tests pass.

- [ ] **Step 3: Run the complete project gate**

```bash
make verify
```

Expected:

- Ruff formatting check passes;
- Ruff lint passes;
- strict mypy passes;
- complete pytest suite passes.

- [ ] **Step 4: Inspect migration and repository state**

```bash
uv run alembic heads
git status --short
git diff --check
git log --oneline --decorate -8
```

Expected:

- exactly one Alembic head: `20260831_0004`;
- only intended documentation changes remain uncommitted before the final docs commit;
- no whitespace errors;
- Task 1 through Task 6 commits are present on the feature branch.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md docs/goal-situation-operator.md docs/superpowers/plans/2026-08-31-milestone-3-goal-situation.md
git commit -m "docs: explain goal and situation operations"
```

- [ ] **Step 6: Re-run final verification after the last commit**

```bash
make verify
git status --short
git diff main...HEAD --stat
```

Expected: verification passes, worktree is clean, and the diff contains only Milestone 3 implementation, tests, migration, and documentation.

- [ ] **Step 7: Request code review**

Invoke `superpowers:requesting-code-review`. Address confirmed findings through additional red-green-refactor cycles and rerun `make verify`. Do not make cosmetic changes solely to satisfy speculative feedback.

- [ ] **Step 8: Push and open the PR**

```bash
git push -u origin codex/milestone-3-goal-situation
gh pr create --base main --head codex/milestone-3-goal-situation --title "Milestone 3: add Goal and Situation domain" --body-file /tmp/eva-milestone-3-pr.md
```

Prepare `/tmp/eva-milestone-3-pr.md` with `apply_patch`, including:

- a concise architecture summary;
- the operator CLI surface;
- deterministic resolver behavior and the explicit Milestone 4 integration boundary;
- migration notes;
- exact verification results.

Confirm the PR URL with:

```bash
gh pr view --json number,url,state,headRefName,baseRefName
```

Expected: an open PR from `codex/milestone-3-goal-situation` into `main`. Stop without merging.

## Final Acceptance Checklist

- [ ] Explicit Goals start ACTIVE with confidence 1 and approval-required autonomy.
- [ ] Inferred Goals can be persisted as CANDIDATE with validated confidence, without inference logic.
- [ ] Goal and Situation operations are fully user/workspace scoped.
- [ ] Goal and Situation lifecycle matrices are enforced and same-state updates are idempotent.
- [ ] Situation snapshots use optimistic version checks and increment exactly once per curated update.
- [ ] A resolver call on the first relevant Gmail Event creates one email-thread Situation.
- [ ] Later same-thread Events attach without overwriting the curated snapshot.
- [ ] Concurrent first resolution converges on one Situation and leaves no orphan.
- [ ] Gmail ingestion by itself creates no Situation.
- [ ] CLI success output is stable JSON; errors and Situation views do not expose raw payloads or secrets.
- [ ] Migration upgrade, downgrade, and re-upgrade pass.
- [ ] Full `make verify` passes after the final commit.
- [ ] Feature branch is pushed and an unmerged PR to `main` is open.
