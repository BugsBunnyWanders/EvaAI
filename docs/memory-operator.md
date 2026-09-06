# Memory and Context Operator Guide

Milestone 5 gives Eva durable structured facts, searchable episodic summaries, and a bounded
Situation-first context builder. Memory creation is explicit in this milestone: Gmail messages,
relevance classifications, and external content do not write memory automatically.

## Mental model

Eva stores two kinds of memory:

- A **fact** is a current structured value such as a communication preference. A fact belongs to
  one Workspace, Goal, or Situation slot. Putting a newer value in the same slot supersedes the
  prior record while retaining history.
- An **episode** is a compact account of a decision, experience, outcome, or Situation summary.
  It is embedded for semantic retrieval and can link to a Situation and several Goals.

Retraction marks a record inactive; it does not delete audit history. Context building starts from
the requested Situation, adds linked active Goals, selects currently valid facts in Situation →
Goal → Workspace order, then retrieves and reranks relevant episodes. The generated context is a
temporary snapshot and is never persisted as authority.

Every command requires both `--user-id` and `--workspace-id`. The repository repeats these filters
and the database uses composite foreign keys so a guessed record ID cannot cross a Workspace
boundary.

## Configuration

Run PostgreSQL and apply the latest migration:

```bash
make db-up
make migrate
```

Fact operations use only PostgreSQL. Episode creation, episode search, and context building also
require the existing server-side key:

```dotenv
EVA_OPENAI_API_KEY=your-key
```

The embedding defaults and conservative retrieval bounds are listed in `.env.example`. Embedding
requests contain only a bounded episode summary or a bounded Situation/Goal query. Eva never sends
OAuth tokens, credentials, attachments, raw provider responses, or the entire mailbox to the
embedding endpoint.

## Structured facts

Create a Workspace-scoped fact:

```bash
uv run eva memory fact put \
  --user-id "$EVA_USER_ID" \
  --workspace-id "$EVA_WORKSPACE_ID" \
  --namespace preferences \
  --key communication_style \
  --value-json '"concise"' \
  --scope-type WORKSPACE \
  --scope-id "$EVA_WORKSPACE_ID" \
  --source-ref operator:local \
  --confidence 1 \
  --idempotency-key preference-communication-v1
```

`--value-json` accepts any JSON value, including an object, list, string, number, Boolean, or null.
For Goal- or Situation-scoped facts, use `GOAL` or `SITUATION` and pass the matching UUID as
`--scope-id`. Reusing an idempotency key with an identical command returns the existing record;
reusing it with different content fails safely.

Inspect and retract facts:

```bash
uv run eva memory fact list --user-id "$EVA_USER_ID" --workspace-id "$EVA_WORKSPACE_ID"
uv run eva memory fact show --user-id "$EVA_USER_ID" --workspace-id "$EVA_WORKSPACE_ID" --memory-id FACT_UUID
uv run eva memory fact retract --user-id "$EVA_USER_ID" --workspace-id "$EVA_WORKSPACE_ID" --memory-id FACT_UUID
```

Credential-like namespaces and keys are rejected deterministically. Secret Manager remains the
only home for OAuth credentials and API keys.

## Episodic memory

Create a decision episode and optionally link it to a Goal and Situation:

```bash
uv run eva memory episode create \
  --user-id "$EVA_USER_ID" \
  --workspace-id "$EVA_WORKSPACE_ID" \
  --type DECISION \
  --summary "Chose the morning flight because it leaves the evening free." \
  --entity travel \
  --goal-id GOAL_UUID \
  --situation-id SITUATION_UUID \
  --importance 0.8 \
  --confidence 1 \
  --source-ref operator:local \
  --idempotency-key travel-decision-v1
```

The embedding call completes before the database transaction begins. A failed provider request
therefore cannot leave a partial episode or Goal link. The command output includes embedding model
metadata and its input digest, but never the vector itself.

Inspect, search, and retract episodes:

```bash
uv run eva memory episode list --user-id "$EVA_USER_ID" --workspace-id "$EVA_WORKSPACE_ID"
uv run eva memory episode show --user-id "$EVA_USER_ID" --workspace-id "$EVA_WORKSPACE_ID" --memory-id EPISODE_UUID
uv run eva memory episode search --user-id "$EVA_USER_ID" --workspace-id "$EVA_WORKSPACE_ID" --query "travel timing"
uv run eva memory episode retract --user-id "$EVA_USER_ID" --workspace-id "$EVA_WORKSPACE_ID" --memory-id EPISODE_UUID
```

Search first filters active rows by User, Workspace, embedding model, and dimension. Application
code then reranks the candidates using semantic similarity, importance, recency, entity overlap,
and linked-Goal overlap.

## Build working context

Preview exactly what a later agent run would receive for a Situation:

```bash
uv run eva context build \
  --user-id "$EVA_USER_ID" \
  --workspace-id "$EVA_WORKSPACE_ID" \
  --situation-id SITUATION_UUID \
  --focus "What should happen next?"
```

The JSON output is bounded, provenance-rich, and includes a canonical SHA-256 digest. When no
episodes exist, Eva skips the embedding request. If semantic retrieval is temporarily unavailable,
the builder still returns the Situation, Goals, and facts with an empty episode list.

## Make inspection shortcuts

After exporting the IDs and optional query, these wrappers provide common read paths:

```bash
export EVA_USER_ID=USER_UUID
export EVA_WORKSPACE_ID=WORKSPACE_UUID
export EVA_MEMORY_ID=EPISODE_UUID
export EVA_MEMORY_QUERY="travel timing"
export EVA_SITUATION_ID=SITUATION_UUID

make memory-fact-list
make memory-episode-list
make memory-episode-show
make memory-episode-search
make context-build
```

All CLI failures render the same content-free operator error. Use local debug logs for the error
class; provider and database error text is deliberately not printed because it may contain secret
or mailbox data.
