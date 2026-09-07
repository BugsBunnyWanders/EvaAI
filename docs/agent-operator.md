# Agent investigation operator guide

Milestone 6 gives Eva a bounded agent for mail that the relevance policy routes to `NOTIFY` or
`INVESTIGATE`. Relevance remains the gate; the full agent is not invoked for `IGNORE` or `RECORD`.

## Runtime architecture

```text
NOTIFY/INVESTIGATE Signal + Situation
    -> same transaction: AgentRun + outbox message
    -> continuous outbox relay -> eva-agent-runs
    -> agent pull consumer claims AgentRun
    -> bounded Situation/Goal/fact/episode context
    -> optional read-only Gmail thread/search tools
    -> strict proposal-only result -> AgentRun SUCCEEDED
```

The agent pull consumer is the fourth loop in `eva worker run`, alongside Gmail ingestion, the
transactional outbox relay, and relevance. Agent Pub/Sub delivery is at-least-once; the logical run
is unique per Signal and agent version, and terminal redelivery does not repeat the OpenAI call.

## Local setup

Create a dedicated topic and subscription once:

```bash
gcloud pubsub topics create eva-agent-runs --project=GCP_PROJECT_ID
gcloud pubsub subscriptions create eva-agent-local \
  --project=GCP_PROJECT_ID \
  --topic=eva-agent-runs
```

Then set these values in the ignored `.env` file:

```dotenv
EVA_RELEVANCE_ENABLED=true
EVA_AGENT_ENABLED=true
EVA_AGENT_TOPIC_ID=eva-agent-runs
EVA_AGENT_SUBSCRIPTION_ID=eva-agent-local
EVA_AGENT_MODEL=gpt-5.6-sol
EVA_AGENT_REASONING_EFFORT=medium
EVA_OPENAI_API_KEY=...
```

Run all four continuous consumers together:

```bash
uv run eva worker run
```

Or run the agent consumer alone while developing:

```bash
uv run eva agent pull
```

Production Terraform creates `eva-agent-runs` and `eva-agent-production`, enables the agent, and
injects the existing OpenAI secret. The existing worker service account already has the required
Pub/Sub, Secret Manager, and Cloud SQL permissions.

## Read-only tools and privacy

The runtime exposes only:

- `gmail_read_thread`, whose thread is derived from the persisted Situation correlation key; and
- `gmail_search`, whose query is bounded and always runs against the same persisted connector.

The model cannot choose a User, Workspace, connector, account, or linked thread. Those authority
values stay in server-side run context. Tool results include selected headers, snippets, bounded
plain text, labels, and attachment metadata. They exclude raw HTML, attachment bytes, OAuth grants,
and unrelated tenant data.

Email and tool content are untrusted evidence. Requests embedded inside mail cannot add tools,
broaden access, write memory, approve an action, or claim user authorization. SDK tracing is
disabled by default because traces could otherwise contain prompts or tool payloads.

## Proposal-only results

A successful AgentRun can propose:

- a Situation snapshot update;
- a concise user notification or question;
- future actions requiring approval;
- memory candidates; or
- a follow-up.

Milestone 7 turns a non-empty notification proposal into a durable Telegram Notification and
delivery request. Situation updates, memory candidates, follow-ups, and action proposals remain
inert. Policy, approval, and external execution arrive in Milestone 8.

For Gmail draft/send, the intended Milestone 8 flow is:

```text
Agent proposes exact recipients, subject, body, and thread
    -> immutable ActionProposal
    -> Eva asks for approval in Telegram
    -> user approves that exact proposal
    -> policy validates the approval and proposal have not changed
    -> a separate Gmail executor drafts or sends once using an idempotency key
    -> the result becomes a new Event
```

The investigating agent will not receive unrestricted Gmail write tools. Approval authorizes only
the immutable proposal shown to the user; changing a recipient, subject, body, or thread requires a
new proposal and new approval. The current connector grants only `gmail.readonly`; Milestone 8 will
add the least-privileged Gmail compose/send scopes through explicit reauthorization before its
separate executor can draft or send mail.

## Inspection and retry

```bash
uv run eva agent run list \
  --user-id USER_UUID \
  --workspace-id WORKSPACE_UUID

uv run eva agent run show \
  --user-id USER_UUID \
  --workspace-id WORKSPACE_UUID \
  --run-id AGENT_RUN_UUID

uv run eva agent run retry \
  --user-id USER_UUID \
  --workspace-id WORKSPACE_UUID \
  --run-id AGENT_RUN_UUID
```

Inspection output includes the structured result, token counts, status, and sanitized tool audit.
It does not include email bodies, prompts, tool results, credentials, raw provider responses, or
hidden reasoning. Manual retry is accepted only for failed runs and creates another outbox delivery
for the same logical AgentRun.

## Failure behavior

- Provider transport, timeout, quota, and transient Gmail/Secret Manager failures use bounded
  exponential retry and Pub/Sub negative acknowledgement.
- Invalid tenant scope, obsolete Signals, revoked/invalid Gmail credentials, invalid structured
  output, and exhausted attempts become terminal.
- A lease prevents concurrent execution, and a stale claimant cannot overwrite a newer result.
- Raw provider errors are never stored or logged.

If runs remain queued, confirm the outbox relay and agent consumer are running and use the same
project/topic/subscription. If a run says `PERMANENT_FAILURE`, correct the connector or configuration
before using the explicit retry command.
