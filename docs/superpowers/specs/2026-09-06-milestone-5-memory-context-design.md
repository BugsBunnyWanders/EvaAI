# Milestone 5 Memory and Context Design

**Date:** 2026-09-06
**Status:** Approved

## Objective

Add Eva's first durable memory system and a bounded Context Builder for later agent runs. The
milestone stores explicit structured facts and compact episodic summaries with provenance, embeds
episodes for semantic retrieval with pgvector, and assembles Situation-first working context without
crossing User or Workspace boundaries.

Milestone 5 provides memory infrastructure. It does not automatically infer preferences from
Gmail, learn from unauthenticated external content, run an investigating agent, or add Telegram
conversation history.

## Decisions

- Memory creation is explicit in this milestone. Automatic learning waits for authenticated user
  conversations and meaningful Situation transitions in later milestones.
- The LLM never writes memory directly. Future model output enters through a typed
  `MemoryProposal`; deterministic application policy decides whether anything is stored.
- Structured facts are versioned records. Replacing a fact inserts a successor and marks the prior
  record superseded in one transaction.
- Episodic memories are compact, append-only summaries. They may be retracted but not edited in
  place.
- Every fact and episode records source type, source reference, confidence, timestamps, and tenant
  scope. Credentials and authorization are never memory.
- Use PostgreSQL JSONB for structured fact values and PostgreSQL `vector(1536)` for episode
  embeddings.
- Use configurable `text-embedding-3-small` with an explicit 1,536-dimensional output. The same
  server-side `EVA_OPENAI_API_KEY` is reused; no additional secret is introduced.
- Use exact pgvector cosine search within a tenant-filtered candidate set for V1. Add an approximate
  index only when data volume justifies its operational and recall trade-offs.
- Context priority is fixed: current Situation, linked active Goals, active structured facts, then
  reranked episodic memories. Conversation context is deferred.
- Semantic similarity is advisory. Application code reranks episodes using similarity, importance,
  recency, entity overlap, and Goal overlap.
- OpenAI calls happen outside database transactions. Only bounded episode summaries and bounded
  Situation/Goal query text are embedded.
- Develop on `codex/milestone-5-memory-context`, push the branch, and deliver a pull request to
  `main`. The user merges it.
- Add comments only around non-obvious privacy, transaction, scoping, idempotency, ranking, and
  supersession invariants.

## Scope

Milestone 5 includes:

- `MemoryFact`, `EpisodicMemory`, and episodic Goal-link persistence
- provenance, confidence, validity windows, supersession, retraction, and idempotency
- hard User/Workspace filtering and composite foreign keys
- provider-neutral embedding contracts and an OpenAI embedding adapter
- pgvector cosine candidate retrieval
- deterministic hybrid episodic reranking
- Situation-first `AgentWorkingContext` construction
- explicit CLI commands for fact and episode management, semantic search, and context preview
- configuration, migrations, unit tests, PostgreSQL integration tests, and operator documentation

Milestone 5 excludes:

- automatic Gmail-to-memory extraction
- automatic preference or Goal inference
- automatic consolidation after Situation transitions
- conversation/message persistence or rolling summaries
- user confirmation UX for model-proposed memory
- agent investigation and tool calls
- Telegram integration
- cross-workspace or global memory
- attachment embedding
- public memory HTTP endpoints
- background embedding, re-embedding, or consolidation workers

## Runtime Flow

### Explicit structured fact

```text
Authenticated operator input
    -> strict MemoryFact command
    -> deterministic safety and scope validation
    -> lock current fact in the same slot
    -> mark predecessor SUPERSEDED + insert successor
    -> return provenance-rich record
```

### Explicit episodic memory

```text
Authenticated operator summary
    -> strict EpisodicMemory command
    -> deterministic safety and scope validation
    -> OpenAI embedding outside transaction
    -> insert episode + Goal links atomically
    -> return content record without embedding values
```

### Context building

```text
Situation ID + optional focus
    -> scoped Situation snapshot
    -> linked active Goals
    -> active, currently valid facts for Workspace/Goals/Situation
    -> bounded semantic query text
    -> query embedding outside transaction
    -> tenant-filtered pgvector candidates
    -> deterministic hybrid reranking
    -> bounded AgentWorkingContext with provenance
```

## Domain Model

### MemoryFact

`memory_facts` stores durable structured knowledge:

- UUID primary key
- `user_id` and `workspace_id`
- normalized `namespace` and `key`
- JSONB `value_json`
- scope type: `WORKSPACE`, `GOAL`, or `SITUATION`
- nullable scoped `goal_id` and `situation_id`, constrained so exactly the selected target exists
- source type: `USER_EXPLICIT`, `USER_BEHAVIOR`, `AGENT_INFERRED`, `EXTERNAL_EVENT`, or
  `SYSTEM_OBSERVED`
- bounded nonblank `source_ref`
- confidence from 0 through 1
- Workspace-scoped idempotency key
- status: `ACTIVE`, `SUPERSEDED`, or `RETRACTED`
- `valid_from` and optional `valid_until`
- optional scoped `supersedes_memory_id`
- created and updated timestamps

Composite foreign keys ensure Workspace, optional Goal, optional Situation, and predecessor all
belong to the same User and Workspace. Separate partial unique indexes allow only one active fact
per namespace/key at each Workspace, Goal, or Situation scope.

Facts are not edited in place. A replacement must target the same logical slot, mark the current
record `SUPERSEDED`, close its validity window when needed, and point the successor to it in the same
transaction. Retraction marks the active record `RETRACTED`; history remains queryable.

### EpisodicMemory

`episodic_memories` stores meaningful experiences and decisions:

- UUID primary key
- `user_id` and `workspace_id`
- type: `DECISION`, `EXPERIENCE`, `OUTCOME`, or `SITUATION_SUMMARY`
- bounded summary
- normalized bounded entity strings
- optional scoped Situation link
- importance and confidence from 0 through 1
- source type and bounded source reference
- Workspace-scoped idempotency key
- occurrence timestamp
- status: `ACTIVE` or `RETRACTED`
- `vector(1536)` embedding
- embedding model, dimensions, input digest, and embedded timestamp
- creation timestamp

`episodic_memory_goals` stores zero or more scoped Goal links. The API and CLI never return raw
embedding vectors.

An episode's human-readable content and provenance are immutable. Retraction preserves it for audit
history but excludes it from retrieval.

### MemoryProposal

A strict provider-neutral proposal shape is introduced for later milestones:

- proposed memory kind
- claim or summary
- proposed namespace/key and scope when applicable
- source type and source reference
- confidence
- reason

Milestone 5 does not add an automatic producer or acceptance worker. This contract prevents a later
agent from bypassing `MemoryService` when memory learning is introduced.

## Safety Policy

`MemoryPolicy` applies before persistence:

- reject credential, secret, token, private-key, password, and authorization namespaces/keys
- reject blank or oversized values and summaries
- reject missing, mismatched, or cross-workspace scope targets
- reject validity windows whose end is not after their start
- reject episode Goal links from another Workspace
- require explicit provenance and confidence for every record
- prevent `EXTERNAL_EVENT` or `AGENT_INFERRED` input from being represented as `USER_EXPLICIT`
- never interpret email instructions as user authorization or preference

The database remains the final scope boundary; policy validation is defense in depth.

## Embedding Boundary

The provider-neutral interface accepts one nonblank bounded string and returns exactly the
configured number of finite floats. The OpenAI adapter uses the embeddings endpoint with:

- model `text-embedding-3-small` by default
- `dimensions=1536`
- no tools and no raw provider response persistence

Only normalized episodic summaries are embedded when stored. Context search embeds a bounded query
constructed from the Situation title, summary, current state, optional focus, and linked Goal titles
and objectives. Raw Events, full email bodies, HTML, attachments, OAuth material, API keys, and
database URLs never enter embedding requests or logs.

Provider failures are surfaced as content-free typed errors. They do not create partially populated
episode rows. Idempotent retries with the same episode key return the existing completed episode.

## Retrieval and Reranking

Candidate retrieval always filters by `user_id`, `workspace_id`, `ACTIVE` status, configured
embedding model, and configured dimensions before ordering by cosine distance. V1 retrieves at most
50 candidates and returns at most eight episodes.

Application reranking uses a stable weighted score:

- semantic similarity: 55%
- importance: 15%
- recency: 10%
- entity overlap: 10%
- linked Goal overlap: 10%

Recency decays smoothly rather than imposing a hard cutoff. Stable IDs break score ties so repeated
builds over unchanged data are deterministic.

## Context Builder

`MemoryContextBuilder.build_for_situation()` requires User, Workspace, and Situation IDs plus an
optional focus string. It produces a strict `AgentWorkingContext` containing:

- context schema version and build timestamp
- scoped Situation snapshot
- linked active Goals, ordered by priority
- active facts in priority order: Situation scope, linked Goal scope, Workspace scope
- reranked episodic memories
- provenance on every included fact and episode
- a SHA-256 digest of the canonical bounded context

Default bounds are 20 facts, 8 episodes, 8,000 serialized fact characters, 6,000 episode-summary
characters, and a 4,000-character embedding query. Bounds are configuration with conservative hard
maximums.

The builder never falls back to another Workspace, never dumps all memory, and never treats
retrieval as authority. If no episodes exist, it skips the embedding call. An embedding outage may
produce structured facts without episodic results, but a caller can require strict episodic
retrieval when needed.

## CLI Surface

The local operator interface adds:

```text
eva memory fact put
eva memory fact list
eva memory fact show
eva memory fact retract

eva memory episode create
eva memory episode list
eva memory episode show
eva memory episode retract
eva memory episode search

eva context build
```

All commands require explicit User and Workspace IDs. Scoped fact commands also require the matching
scope target. Outputs are stable JSON, omit embedding vectors, and never expose secrets or raw
provider errors.

## Configuration

Milestone 5 adds:

```dotenv
EVA_MEMORY_EMBEDDING_MODEL=text-embedding-3-small
EVA_MEMORY_EMBEDDING_DIMENSIONS=1536
EVA_MEMORY_EMBEDDING_INPUT_MAX_CHARS=4000
EVA_MEMORY_FACT_LIMIT=20
EVA_MEMORY_FACT_TOTAL_CHARS=8000
EVA_MEMORY_EPISODE_CANDIDATE_LIMIT=50
EVA_MEMORY_EPISODE_LIMIT=8
EVA_MEMORY_EPISODE_TOTAL_CHARS=6000
```

The existing `EVA_OPENAI_API_KEY` is required only for commands that create/search episodic memory
or build semantic context. Structured fact operations remain available without a provider call.

## Transactions and Concurrency

- Network calls never occur inside database transactions.
- Fact replacement locks the active slot before superseding it and inserting the successor.
- Partial unique indexes resolve races that pass application checks; conflicts return a typed
  concurrency error rather than overwriting history.
- Episode idempotency is scoped to User and Workspace. Replaying the same logical create command
  returns the existing episode without another embedding call when it already exists.
- Episode insertion and Goal links commit atomically.
- Context is a read snapshot. It is not persisted as memory and grants no permissions.

## Testing

Unit tests cover:

- strict types, normalization, bounds, and safety policy
- fact slot selection, supersession, retraction, and idempotent replay
- embedding validation and content-free failures
- hybrid ranking, stable ties, recency, entity overlap, and Goal overlap
- context ordering, bounds, canonical digest, outage degradation, and workspace filtering
- CLI parsing and secret-safe output

PostgreSQL integration tests cover:

- migration upgrade/downgrade shape
- composite scope constraints and partial active-fact uniqueness
- fact history under concurrent replacement
- episode/Goal atomicity and idempotency
- pgvector cosine retrieval with strict User/Workspace filters
- no embedding vectors in returned records

An OpenAI live call is not part of the automated test suite. The operator guide includes an
explicit, opt-in smoke test using synthetic content.

## Deliverables

- Alembic revision `20260906_0006`
- memory ORM models, domain types, policy, repository, service, embedding adapter, and Context
  Builder
- memory/context CLI commands and runtime composition
- unit and PostgreSQL integration tests
- `.env.example`, README architecture update, and `docs/memory-operator.md`
- implementation plan with red-green-refactor tasks
- verified branch and pull request to `main`
