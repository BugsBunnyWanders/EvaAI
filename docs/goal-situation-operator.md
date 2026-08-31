# Goal and Situation Operator Guide

Milestone 3 gives Eva durable intent and operational context. It adds scoped Goal and
Situation services, deterministic Gmail-thread correlation, and a small local operator CLI.
It does not yet decide which incoming email is relevant.

## Mental model

- An **Event** is immutable history: something happened, such as Gmail receiving a message.
- A **Goal** is longer-lived intent: something the user wants to achieve or maintain.
- A **Situation** is a bounded case currently unfolding, such as one Gmail conversation.

A Situation may link many Events and many Goals. Its compact snapshot answers “what is
happening now?” while the linked Events remain the historical source records.

Operator-created Goals are `USER_EXPLICIT`, start `ACTIVE`, and have confidence `1`.
The domain also supports future `AGENT_INFERRED` Goals; those start as `CANDIDATE` and must
be deliberately accepted or abandoned. Milestone 3 does not infer Goals automatically.

Every Goal has this fixed autonomy policy:

```json
{"mode":"REQUIRE_APPROVAL"}
```

That field grants no execution authority. Eva cannot approve or perform an action merely
because a Goal exists.

## Before using the CLI

Start PostgreSQL, apply migrations, and obtain the local scope IDs created in Milestone 2:

```bash
make db-up
make migrate
uv run eva scope create --display-name "Saswat Ray" --workspace-name personal
export EVA_USER_ID=USER_UUID_FROM_OUTPUT
export EVA_WORKSPACE_ID=WORKSPACE_UUID_FROM_OUTPUT
```

All commands require both IDs. This is intentional: repositories enforce exact User and
Workspace ownership instead of inferring scope from a provider account or free-form text.

## Goal commands

Create an achievement Goal:

```bash
uv run eva goal create \
  --user-id "$EVA_USER_ID" \
  --workspace-id "$EVA_WORKSPACE_ID" \
  --title "Book conference travel" \
  --objective "Attend the September engineering conference" \
  --domain travel \
  --mode ACHIEVE \
  --priority 80 \
  --success-criterion "Flights are booked" \
  --success-criterion "Hotel is booked" \
  --constraints-json '{"budget":"bounded","currency":"INR"}'
```

Create a maintenance Goal:

```bash
uv run eva goal create \
  --user-id "$EVA_USER_ID" \
  --workspace-id "$EVA_WORKSPACE_ID" \
  --title "Keep important email current" \
  --objective "Review and resolve time-sensitive personal email" \
  --domain email \
  --mode MAINTAIN
```

List Goals, optionally repeating a status filter:

```bash
uv run eva goal list \
  --user-id "$EVA_USER_ID" \
  --workspace-id "$EVA_WORKSPACE_ID" \
  --status ACTIVE \
  --status PAUSED \
  --limit 50
```

Show or update one Goal:

```bash
export EVA_GOAL_ID=GOAL_UUID

uv run eva goal show \
  --user-id "$EVA_USER_ID" \
  --workspace-id "$EVA_WORKSPACE_ID" \
  --goal-id "$EVA_GOAL_ID"

uv run eva goal update \
  --user-id "$EVA_USER_ID" \
  --workspace-id "$EVA_WORKSPACE_ID" \
  --goal-id "$EVA_GOAL_ID" \
  --title "Conference travel booked" \
  --status COMPLETED
```

`goal update` also accepts `--objective`, `--domain`, `--mode`, `--priority`, repeated
`--success-criterion`, `--constraints-json`, `--parent-goal-id`, and `--clear-parent`.
Supplying success criteria replaces the full criteria list. `--parent-goal-id` and
`--clear-parent` are mutually exclusive.

Successful commands print exactly one JSON document. Goal lists are ordered by priority
descending, creation time ascending, then UUID.

## Situation commands

Situation inspection is read-only in this milestone:

```bash
uv run eva situation list \
  --user-id "$EVA_USER_ID" \
  --workspace-id "$EVA_WORKSPACE_ID" \
  --lifecycle OPEN \
  --lifecycle WAITING_EXTERNAL \
  --limit 50

export EVA_SITUATION_ID=SITUATION_UUID

uv run eva situation show \
  --user-id "$EVA_USER_ID" \
  --workspace-id "$EVA_WORKSPACE_ID" \
  --situation-id "$EVA_SITUATION_ID"
```

The show response includes the current Situation plus safe Event-link and Goal-link
metadata. It does not include raw Event payloads, Gmail bodies or headers, OAuth material,
connector secrets, or database details.

Situation lists place unresolved cases before terminal ones, then order by attention and
latest real-world activity.

## Gmail-thread correlation boundary

The resolver supports one strong key in Milestone 3: `gmail-thread:<thread-id>`. The first
relevant Gmail Event for a thread creates an `EMAIL_THREAD` Situation and atomically reserves
that key within the Workspace. Later Events with the same thread key reuse the Situation and
advance its last activity without overwriting a curated snapshot. Concurrent resolution is
protected by the database uniqueness constraint, so duplicate Situations do not survive.

Gmail ingestion alone does **not** call this resolver. Receiving or synchronizing email still
creates immutable Events only. Milestone 4 will evaluate relevance and invoke resolution for
the Events that deserve operational attention. Therefore an empty `situation list` after new
mail is expected during Milestone 3.

## Lifecycle rules

Requesting the current state again is idempotent. `COMPLETED`, `RESOLVED`, and `ABANDONED`
records are terminal and cannot be silently reopened.

| Goal status | Allowed next status |
| --- | --- |
| `CANDIDATE` | `ACTIVE`, `ABANDONED` |
| `ACTIVE` | `PAUSED`, `COMPLETED`, `ABANDONED` |
| `PAUSED` | `ACTIVE`, `COMPLETED`, `ABANDONED` |
| `COMPLETED` | None |
| `ABANDONED` | None |

| Situation lifecycle | Allowed next lifecycle |
| --- | --- |
| `OPEN` | `ACTIVE`, `WAITING_USER`, `WAITING_EXTERNAL`, `RESOLVED`, `ABANDONED` |
| `ACTIVE` | `WAITING_USER`, `WAITING_EXTERNAL`, `RESOLVED`, `ABANDONED` |
| `WAITING_USER` | `ACTIVE`, `WAITING_EXTERNAL`, `RESOLVED`, `ABANDONED` |
| `WAITING_EXTERNAL` | `ACTIVE`, `WAITING_USER`, `RESOLVED`, `ABANDONED` |
| `RESOLVED` | None |
| `ABANDONED` | None |

Situation snapshot writes use optimistic concurrency. A caller supplies the version it read;
the write succeeds only if that version is still current, then increments it once. A stale
caller receives a version conflict instead of overwriting newer context. Snapshot mutation is
a service interface for later milestones and is not exposed by the Milestone 3 CLI.

## Troubleshooting

- `eva: command failed`: confirm the User, Workspace, Goal, and Situation IDs belong to the
  same scope. The CLI intentionally does not reveal whether a foreign scoped record exists.
- A Goal transition fails: compare the current status with the table above. Terminal Goals
  cannot be reopened; create a new Goal if the user's intent genuinely changed.
- A snapshot update reports a stale version: read the Situation again, reconcile the newer
  snapshot, and retry with its current version. Do not blindly repeat the stale write.
- `--constraints-json` is rejected: pass one valid JSON object, quoted for the shell. Arrays,
  primitives, malformed JSON, and objects larger than 8 KiB are rejected.
- A Situation is absent after Gmail sync: this is expected until Milestone 4 relevance routing
  is implemented. Do not manually run the resolver for every email.
- A list is rejected: `--limit` must be from 1 through 100 and filters must use the exact enum
  values shown by `uv run eva goal --help` or `uv run eva situation --help`.

