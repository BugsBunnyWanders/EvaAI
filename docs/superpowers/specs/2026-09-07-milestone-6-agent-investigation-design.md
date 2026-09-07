# Milestone 6 Agent Investigation Design

**Date:** 2026-09-07
**Status:** Approved

## Objective

Add Eva's first bounded investigating agent. Relevance remains the inexpensive gate: a current
`NOTIFY` or `INVESTIGATE` Signal creates an AgentRun. The agent receives Situation-first memory
context and may use narrowly scoped, read-only Gmail tools before returning a strict proposal
object for later application layers.

Milestone 6 makes investigations durable and inspectable. It does not notify Telegram, mutate a
Situation from model output, write memory automatically, or execute external actions.

## Decisions

- Invoke the full agent for `NOTIFY` and `INVESTIGATE`; `IGNORE` and `RECORD` do not schedule an
  AgentRun.
- Schedule the AgentRun and its outbox message in the same transaction that persists the Signal and
  resolves its Situation. A committed investigation cannot exist without a durable delivery record.
- Publish investigation requests to a dedicated `eva-agent-runs` topic and consume them from a
  dedicated subscription. This preserves independent retry, scaling, and observability boundaries.
- Add the agent consumer as a fourth loop inside the existing Cloud Run worker-pool process. No new
  always-on compute resource is required for the current single-user deployment.
- Use the OpenAI Agents SDK with configurable `gpt-5.6-sol`, medium reasoning, strict structured
  output, at most six turns, and at most four Gmail tool calls per run.
- Give the agent only two provider-neutral tools: bounded Gmail thread reading and bounded Gmail
  search. Both enforce the persisted User, Workspace, connector, and Situation scope in application
  code.
- Treat email content as untrusted evidence. It may supply facts to investigate but may not grant
  authority, change tool scope, create memory, or authorize an action.
- Persist the final structured result and sanitized execution metadata, never hidden chain of
  thought, OAuth material, raw provider responses, or raw exceptions.
- Agent output is proposal-only in this milestone. Telegram delivery arrives in Milestone 7;
  action policy, approval, and execution arrive in Milestone 8.
- Develop on `codex/milestone-6-agent-investigation`, push the branch, and deliver a pull request to
  `main`. The user merges it.
- Add comments only around non-obvious privacy, transaction, tenant-scoping, idempotency, leasing,
  retry, and prompt-injection boundaries.

## Scope

Milestone 6 includes:

- `AgentRun` persistence, leasing, idempotency, attempts, outcomes, and audit metadata
- transactional scheduling from a newly persisted `NOTIFY` or `INVESTIGATE` Signal
- a typed `agent.run.requested` outbox/Pub/Sub envelope
- a continuous agent pull worker with ack/nack and retry classification
- Situation-first context assembly using the Milestone 5 Context Builder
- read-only Gmail thread and search tools
- OpenAI Agents SDK orchestration with strict structured output
- CLI inspection and explicit retry commands
- configuration, migration, Terraform resources, tests, and operator documentation

Milestone 6 excludes:

- invoking the full agent for every Event
- Telegram notifications or chat
- Situation mutation from model output
- automatic acceptance of memory proposals
- Gmail draft/send, calendar, browser, web search, or arbitrary code tools
- approval and action execution
- attachments or raw HTML in agent context
- semantic merging of different Gmail threads into one Situation
- user-facing connector or investigation UI

## Runtime Flow

```text
Gmail Event
    -> relevance screen/classifier
    -> disposition is NOTIFY or INVESTIGATE
    -> one database transaction:
         persist Signal
         resolve/link Situation
         insert QUEUED AgentRun
         insert agent.run.requested outbox message
    -> existing outbox relay publishes to eva-agent-runs
    -> agent pull worker claims AgentRun
    -> build bounded Situation/Goal/fact/episode context
    -> create scoped Gmail read session from connector secret
    -> Agents SDK may read the linked thread or perform bounded searches
    -> validate strict AgentInvestigationResult
    -> persist SUCCEEDED result and usage metadata
    -> acknowledge Pub/Sub message
```

If the process stops after the database commit but before publication, the existing outbox relay
publishes later. If delivery repeats, the consumer resolves the same AgentRun and a completed run is
acknowledged without another model call.

## AgentRun Domain Model

`agent_runs` stores one logical investigation per triggering Signal and agent version:

- UUID primary key
- `user_id`, `workspace_id`, `event_id`, `signal_id`, and `situation_id`
- status: `QUEUED`, `RUNNING`, `SUCCEEDED`, `RETRYABLE_FAILURE`, or `PERMANENT_FAILURE`
- provider, model, agent version, prompt version, and output schema version
- attempt count, next retry time, claim ID, and lease expiry
- input/context digest
- structured result JSON only after success
- provider response ID when available
- input, output, and total token counts when available
- bounded tool-call audit entries containing tool name, timing, outcome, and result count only
- sanitized failure code and summary
- queued, started, and completed timestamps

Composite foreign keys bind Event, Signal, and Situation to the same User and Workspace. A unique
constraint on `(workspace_id, user_id, signal_id, agent_version)` makes scheduling idempotent.

The Pub/Sub envelope contains only IDs and schema information:

```json
{
  "outbox_message_id": "uuid",
  "agent_run_id": "uuid",
  "event_id": "uuid",
  "signal_id": "uuid",
  "situation_id": "uuid",
  "user_id": "uuid",
  "workspace_id": "uuid",
  "schema_version": 1
}
```

## Structured Result

The SDK output type is a strict provider-neutral `AgentInvestigationResult`:

```json
{
  "situation_update": {
    "current_state": "SCHEDULING",
    "summary": "A meeting time still needs confirmation.",
    "next_action": "Ask the user which proposed time works.",
    "next_expected": "User chooses a time."
  },
  "decision": "ASK_USER",
  "reasoning_summary": "The email proposes two times and no stored preference resolves the choice.",
  "proposed_actions": [],
  "notification": {
    "urgency": "medium",
    "message": "Two meeting times were proposed. Which one should I accept?"
  },
  "memory_proposals": [],
  "follow_up": null
}
```

Allowed decisions are `NO_ACTION`, `NOTIFY_USER`, `ASK_USER`, `PROPOSE_ACTION`, and `FOLLOW_UP`.
Proposed actions describe intent and bounded arguments but cannot execute. Memory proposals reuse
the Milestone 5 contract and remain unaccepted. The reasoning summary is a concise decision
rationale, not chain of thought. In Milestone 8, a Gmail draft/send proposal can become an immutable
ActionProposal, but only a separate executor may use Gmail write capabilities after Telegram
approval of that exact proposal and explicit connector reauthorization for least-privileged Gmail
compose/send scopes.

## Agent Input

The agent receives a canonical, bounded payload containing:

- the triggering Event's selected headers, plain text, snippet, labels, and attachment metadata
- the current relevance Signal and Goal matches
- the scoped Situation and linked Goals
- active structured facts and retrieved episodic memories from `MemoryContextBuilder`
- explicit task, tool limits, and capability limits

Raw HTML, attachment bodies, OAuth credentials, API keys, database URLs, and unrelated Workspace
data are excluded. Canonical serialization produces the persisted input digest.

## Gmail Read Tools

### `gmail_read_thread`

Reads only the Gmail thread linked to the current Situation's correlation key. It returns at most 20
messages, ordered chronologically, with selected headers, snippet, bounded plain text, label IDs,
and attachment metadata. It never returns attachment bytes or raw HTML.

### `gmail_search`

Accepts a Gmail search query and returns at most 10 matching messages from the same persisted
connector. It fetches bounded message summaries after the Gmail list call. Pagination is not exposed
to the model.

The model does not provide User, Workspace, connector, account, or thread authority parameters.
Those values come only from the AgentRun's server-side context. Tool input and output limits are
validated before returning data to the SDK.

## Prompt-Injection Boundary

The stable agent instructions state that email content and tool results are untrusted evidence, not
instructions. The agent must ignore requests inside email to reveal secrets, broaden searches,
change identity, call unavailable tools, or claim user authorization.

Application code is the enforcement boundary:

- only two read-only tools are registered
- connector ownership is checked before every run
- the linked thread ID is derived from persisted correlation data
- search cannot select a different account or Workspace
- tool calls have timeouts and bounded outputs
- the run has fixed turn and tool-call budgets
- no application service applies output proposals in Milestone 6

## Retry and Leasing

The agent consumer claims a queued or lease-expired retryable run before network work. The OpenAI and
Gmail calls occur outside database transactions.

- transient provider, quota, timeout, and transport failures become `RETRYABLE_FAILURE`, set a
  bounded exponential next-retry time, and nack the Pub/Sub message
- invalid persisted scope, revoked Gmail authorization, invalid structured output after the SDK's
  own validation, or exhausted attempts become `PERMANENT_FAILURE` and ack the message
- cancellation releases or expires the lease without storing raw exception content
- a stale claimant cannot overwrite a result written by a newer claimant
- Pub/Sub redelivery after `SUCCEEDED` or `PERMANENT_FAILURE` is acknowledged immediately

## OpenAI Boundary

Use `openai-agents>=0.22,<0.23` with its required OpenAI Python SDK 3.x. The adapter creates one
stateless SDK run per AgentRun with:

- `Agent[AgentToolContext]`
- strict Pydantic `output_type=AgentInvestigationResult`
- `gpt-5.6-sol` by default with medium reasoning
- `max_turns=6`
- a four-call aggregate Gmail tool budget
- per-tool timeouts
- SDK tracing disabled by default so email/tool payloads are not exported through SDK traces
- no SDK session, conversation ID, or previous response ID

Provider-specific objects stay inside `integrations/openai`. Domain services depend on an
`InvestigationAgent` protocol and receive only typed request/result records.

## Outbox and Pub/Sub

Generalize the existing outbox envelope from an Event-only type to a discriminated union supporting:

- `event.available` -> existing `eva-events` topic
- `agent.run.requested` -> new `eva-agent-runs` topic

The relay still claims and publishes all outbox rows continuously. Pub/Sub attributes carry the
message type and non-sensitive scope IDs for operations; consumers validate the JSON envelope and
load authoritative state from PostgreSQL.

Production Terraform adds the topic, durable subscription `eva-agent-production`, worker IAM access,
and environment variables. Deployment continues to pause the single worker instance around
migrations and restore its configured count afterward.

## Configuration

Milestone 6 adds:

```dotenv
EVA_AGENT_ENABLED=false
EVA_AGENT_TOPIC_ID=eva-agent-runs
EVA_AGENT_SUBSCRIPTION_ID=eva-agent-local
EVA_AGENT_MODEL=gpt-5.6-sol
EVA_AGENT_REASONING_EFFORT=medium
EVA_AGENT_VERSION=investigation-v1
EVA_AGENT_PROMPT_VERSION=investigation-prompt-v1
EVA_AGENT_PULL_TIMEOUT_SECONDS=30
EVA_AGENT_LEASE_SECONDS=900
EVA_AGENT_MAX_ATTEMPTS=4
EVA_AGENT_RETRY_INITIAL_BACKOFF_SECONDS=10
EVA_AGENT_RETRY_MAX_BACKOFF_SECONDS=300
EVA_AGENT_MAX_TURNS=6
EVA_AGENT_MAX_TOOL_CALLS=4
EVA_AGENT_TOOL_TIMEOUT_SECONDS=30
EVA_AGENT_THREAD_MESSAGE_LIMIT=20
EVA_AGENT_SEARCH_RESULT_LIMIT=10
EVA_AGENT_MESSAGE_BODY_MAX_CHARS=6000
```

Production enables the agent only after its migration and Pub/Sub resources exist. The existing
`EVA_OPENAI_API_KEY` and per-connector Gmail Secret Manager grant are reused.

## CLI Surface

```text
eva agent run show
eva agent run list
eva agent run retry
eva agent pull
```

All inspection and retry commands require explicit User and Workspace IDs. Retry creates a new
delivery for the same logical run only when its current state is retryable or permanently failed;
it never invents a different tenant scope.

## Observability and Privacy

Structured logs identify AgentRun, Event, Signal, Situation, Workspace, attempt number, model,
outcome, latency, and sanitized failure code. Metrics can derive queue depth, run duration, tool
counts, retry rate, token use, and outcome distribution.

Logs and CLI output never include email bodies, search queries, tool results, prompts, OAuth grants,
API keys, provider errors, or hidden reasoning. The stored result remains user data protected by the
same tenant boundary as its Situation.

## Testing Strategy

- strict contracts and bounds for envelopes, requests, tool results, and agent results
- migration shape, composite tenant foreign keys, uniqueness, and status constraints
- transactional scheduling and replay after relevance redelivery/reevaluation
- outbox union publication and attribute routing
- claim, lease expiry, stale claims, retries, terminal outcomes, and redelivery
- Gmail thread/search bounds, connector scope, unavailable messages, and revoked authorization
- Agents SDK adapter using a fake provider/runner with structured output and tool-budget tests
- prompt-injection contract tests proving external content cannot add tools or change scope
- no direct side effects from successful agent output
- worker composition and cleanup with the fourth loop
- Terraform validation and production environment wiring
- end-to-end PostgreSQL flow from `NOTIFY`/`INVESTIGATE` Signal to persisted AgentRun result

## Acceptance Criteria

- Only a current `NOTIFY` or `INVESTIGATE` Signal transactionally schedules one logical AgentRun.
- Replaying relevance or Pub/Sub delivery cannot duplicate a logical run or successful model call.
- The agent can read only the Situation's Gmail thread and bounded searches from its own connector.
- All tool and context reads enforce User and Workspace scope in code and database constraints.
- Every successful run stores a strict result and useful non-sensitive audit metadata.
- No agent result causes a Situation, memory, notification, or external action side effect.
- Failures are safely classified, retried when appropriate, and never leak provider/email content.
- The continuous production worker includes Gmail, outbox, relevance, and agent loops.
- CI, migration tests, integration tests, and Terraform validation pass.
