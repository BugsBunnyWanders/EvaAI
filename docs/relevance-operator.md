# Relevance engine operator guide

Milestone 4 evaluates canonical Events against Eva's active Goals and existing Situations. It
stores every decision as an immutable, versioned Signal so a past `IGNORE` or `RECORD` can still be
found and explicitly re-evaluated when the user's priorities change.

## Runtime architecture

Gmail uses two distinct Pub/Sub paths:

- `eva-gmail-notifications` wakes the Gmail ingestion worker. Gmail notifications contain a mailbox
  identity and history cursor, not normalized email content.
- `eva-events` carries Eva's canonical `event.available` envelopes after ingestion has committed an
  Event and its OutboxMessage.

Run these independent processes in separate local terminals:

```bash
uv run eva gmail pull
uv run eva events relay
uv run eva relevance pull
```

In deployment, a process manager must keep all required workers running independently. The Event
Relay continuously drains pending outbox rows; the relevance consumer never scans or publishes the
outbox. This relay is necessary because PostgreSQL and Pub/Sub cannot participate in one atomic
transaction: publishing synchronously after the database commit could lose a message if the
process crashes between those operations. A future direct post-commit publish may reduce latency,
but it is only a fast path and cannot replace the durable relay.

The relevance flow is:

```text
Event -> deterministic screening -> bounded context -> OpenAI structured classification
      -> Eva routing policy -> durable Signal -> optional Situation
```

Exact ignore rules and malformed/unsupported/duplicate checks run first. Otherwise, the model
recommends relevance, importance, urgency, category, Goal matches, and an action. Application code
owns the final `IGNORE`, `RECORD`, `NOTIFY`, or `INVESTIGATE` route. Only the latter two create or
reuse a Gmail-thread Situation. This milestone does not send Telegram notifications, launch an
investigator, call tools, or execute actions.

## GCP setup

Create the internal topic and relevance subscription once, replacing `GCP_PROJECT_ID`. Skip topic
creation if `eva-events` already exists.

```bash
gcloud pubsub topics create eva-events \
  --project=GCP_PROJECT_ID

gcloud pubsub subscriptions create eva-relevance-local \
  --project=GCP_PROJECT_ID \
  --topic=eva-events
```

The runtime uses Application Default Credentials for Pub/Sub. In deployment, provide
`EVA_OPENAI_API_KEY` through Secret Manager/runtime secret injection; do not commit it. A local
developer may place it in the ignored `.env` file.

Relevance is deliberately disabled by default. Start from `.env.example`, set
`EVA_RELEVANCE_ENABLED=true`, configure `EVA_PUBSUB_PROJECT_ID`, and supply the API key before
running relevance classification. Enabling the feature, changing a Goal, changing a model, or
running a migration never starts a historical scan.

## Privacy boundary

The OpenAI request contains only:

- sender display name/address, subject, snippet, normalized labels, and at most 4,000 characters of
  plain-text email body;
- at most 20 bounded active Goals; and
- at most five bounded candidate Situations.

HTML, attachments, raw headers, OAuth material, connector metadata, API keys, database URLs, full
prompts, and raw model responses are excluded from persistence and logs. Requests use strict
structured parsing, no tools, and `store=False`. That flag asks the API not to store the response;
it is not a broader claim about all provider retention or abuse-monitoring policies.

## Operator commands

Inspect the current decision or its full decision/attempt history without exposing Event content:

```bash
uv run eva relevance show --user-id USER_UUID --workspace-id WORKSPACE_UUID --event-id EVENT_UUID
uv run eva relevance history --user-id USER_UUID --workspace-id WORKSPACE_UUID --event-id EVENT_UUID
```

Explicit re-evaluation appends and supersedes; it never edits the Event or deletes old Signals. The
command prints the evaluation key to stderr before provider work. Reuse that key to replay safely.

```bash
uv run eva relevance reevaluate \
  --user-id USER_UUID \
  --workspace-id WORKSPACE_UUID \
  --event-id EVENT_UUID \
  --reason "A newly active goal changes this decision" \
  --idempotency-key EVALUATION_UUID
```

Omit `--idempotency-key` to generate a new UUID. Backfill is explicit, tenant-scoped, oldest-first,
and processes one selected batch only (default 50, maximum 100):

```bash
uv run eva relevance backfill --user-id USER_UUID --workspace-id WORKSPACE_UUID --limit 50
```

All one-shot commands emit stable JSON. Make wrappers are also available after exporting the
literal `EVA_USER_ID`, `EVA_WORKSPACE_ID`, `EVA_EVENT_ID`, `EVA_RELEVANCE_REASON`, and (for the Make
re-evaluation wrapper) `EVA_RELEVANCE_IDEMPOTENCY_KEY` values.

## Retries and troubleshooting

Transient provider and rejected structured outputs are recorded as content-free attempts and
retried with bounded exponential backoff and jitter. Exhaustion becomes a visible permanent
review-required attempt and is acknowledged by the pull worker to avoid a hot Pub/Sub loop. Use a
new explicit re-evaluation key after correcting the cause.

- **Relevance processing is disabled:** set `EVA_RELEVANCE_ENABLED=true` for the relevance worker
  or model-backed operator commands.
- **OpenAI configuration is incomplete:** inject a nonblank `EVA_OPENAI_API_KEY` and restart only
  the relevance process.
- **Pub/Sub configuration is incomplete:** set `EVA_PUBSUB_PROJECT_ID` and verify the topic and
  subscription exist. The relay itself does not require relevance to be enabled.
- **Provider outage or failed attempts:** inspect `relevance history`; keep the relay running, then
  explicitly re-evaluate once the provider is healthy.
- **Events are stored but not classified:** confirm both `eva events relay` and
  `eva relevance pull` are continuously running and using the same project/topic/subscription.
