# Milestone 7 Telegram Conversation Design

**Date:** 2026-09-07
**Status:** Approved

## Objective

Give Eva a durable, authenticated Telegram interface that is both proactive and reactive.

- Eva can initiate a conversation when an investigation produces a user-worthy notification.
- The user can reply to that notification and remain in the same Situation context.
- The user can message Eva at any time to start or continue a general conversation.
- Every inbound update, model turn, and outbound notification survives retries and process restarts.

Milestone 7 completes the conversational portion of the first vertical slice. It does not grant Gmail
write authority. Drafting, exact-proposal approval, and sending remain Milestone 8.

## Product Behavior

### Proactive conversation

When a successful Milestone 6 AgentRun contains a notification proposal, Eva persists a Telegram
Notification and schedules it for delivery. The alert is linked to the AgentRun's Situation. If the
user replies to that Telegram message, Eva resolves the provider reply ID back to the Notification
and Situation before invoking the conversation agent.

Example:

```text
Eva: A recruiter proposed Tuesday or Wednesday. Wednesday afternoon appears free.
User: Tell me more about the role.
Eva: The email describes a Staff Backend role focused on distributed systems...
```

### User-initiated conversation

After the account is paired, any ordinary private message starts a general conversation if none is
active, or continues the current one. The user does not need to wait for Eva or use a special command.

`/new` starts a fresh general conversation. The first ordinary message also creates one
automatically. General conversations have their own `TELEGRAM_CHAT` Situation so they can use the
same Situation-first context and memory architecture as proactive conversations.

Replying directly to a proactive alert takes precedence over the current general conversation and
binds the turn to the alert's Situation. Later ordinary messages continue the most recently active
conversation. This rule is deterministic and visible in stored state.

### Real-time expectation

Telegram webhooks are acknowledged immediately after durable ingestion. A background conversation
worker normally answers within seconds. Milestone 7 sends complete text messages rather than
streaming partial tokens. This prevents Telegram retries, model latency, or Cloud Run restarts from
losing a turn.

## Decisions

- Support full bidirectional text conversation in Milestone 7, not only inbound-message storage.
- Accept only private Telegram chats in the first release.
- Authenticate the webhook with Telegram's secret-token header and compare it in constant time.
- Map identity by immutable Telegram numeric user ID and chat ID, never username or display name.
- Pair accounts with a high-entropy, expiring, single-use `/start <code>` link.
- Keep webhook work bounded to validation and durable database writes. It never calls OpenAI,
  Gmail, or Telegram synchronously.
- Store canonical Eva-owned conversation history and build a bounded prompt for every turn. Do not
  use provider-managed conversation IDs, SDK sessions, or `previous_response_id` as authoritative
  memory.
- Give reactive conversation the existing bounded Gmail read tools. Tool scope is derived from the
  authenticated User, Workspace, connector, and resolved Situation; the model cannot choose a
  different account.
- Reuse the transactional outbox for both requested conversation turns and outbound delivery.
- Deliver all proactive and reactive messages through the same idempotent Notification pipeline.
- Add two dedicated Pub/Sub topics and consumers inside the existing worker-pool process: Telegram
  conversation turns and Telegram delivery.
- Keep callbacks durable but do not interpret them as action approval until Milestone 8.
- Develop on `codex/milestone-7-telegram-notifications`, push the branch, and deliver a pull request
  to `main`. The user merges it.
- Add comments only around non-obvious authentication, privacy, transaction, tenant-scoping,
  idempotency, correlation, leasing, and prompt-injection boundaries.

## Scope

Milestone 7 includes:

- Telegram bot account pairing and revocation
- verified FastAPI webhook ingestion
- canonical inbound Telegram Events with provider idempotency
- proactive Notification creation from successful agent output
- idempotent Telegram text delivery and provider-message correlation
- general and Situation-linked text conversations
- a durable conversation-turn worker and provider-neutral conversational agent
- Situation-first memory context and scoped Gmail read tools for conversation
- CLI commands for pairing, webhook setup, notification inspection, and local worker pulls
- configuration, Secret Manager, Pub/Sub, Terraform, tests, and operator documentation

Milestone 7 excludes:

- Gmail drafts, sends, labels, deletes, or any other write operation
- approval authorization or Action execution
- applying Situation updates or memory proposals from model output
- Telegram groups, channels, media, voice, locations, contacts, or file attachments
- partial-token streaming, typing indicators, reactions, or message editing
- a public web UI for account connection
- autonomous follow-up scheduling

## Architecture

```text
Proactive path

AgentRun succeeds with notification proposal
    -> one database transaction:
         mark AgentRun SUCCEEDED
         create deduplicated Notification linked to Situation
         insert notification.delivery.requested outbox message
    -> continuous outbox relay
    -> eva-telegram-delivery topic
    -> Telegram delivery worker
    -> Telegram Bot API sendMessage
    -> persist SENT + provider message ID

Reactive path

User sends Telegram message
    -> Telegram webhook
    -> validate secret header and paired numeric identity
    -> persist canonical Event + event.available outbox row
    -> HTTP 200
    -> continuous outbox relay
    -> eva-telegram-turns topic
    -> conversation worker resolves conversation/Situation
    -> load bounded Situation, goals, memory, and conversation history
    -> conversational agent may call scoped read-only Gmail tools
    -> one database transaction:
         persist assistant turn
         create deduplicated Notification
         insert notification.delivery.requested outbox message
    -> existing delivery path sends the response
```

All external calls happen outside database transactions. Every transition before an external call
is durable and retryable.

## Telegram Identity and Pairing

`telegram_accounts` maps one Telegram private chat to an Eva User and Workspace:

- UUID primary key
- `user_id` and `workspace_id`
- numeric `telegram_user_id` and `chat_id`
- optional display metadata for operator visibility only
- status: `ACTIVE` or `REVOKED`
- paired, revoked, and updated timestamps

Composite tenant keys prevent cross-Workspace use. Active provider user IDs and chat IDs are unique.
Authorization always checks both numeric identifiers so forwarded content or a changed username
cannot impersonate the user.

`telegram_pairing_codes` contains:

- UUID, User, and Workspace scope
- a SHA-256 digest of a cryptographically random token; plaintext is never stored
- expiry and single-use consumption timestamps
- optional consuming Telegram account ID

The operator creates a short-lived link with `eva telegram pairing create`. The CLI displays the
plaintext token once. The user opens `https://t.me/<bot_username>?start=<token>`. A valid `/start`
update atomically consumes the code and creates or reactivates the mapping. Unknown accounts may do
nothing except present a valid pairing token.

For the current personal deployment, this links the existing local User and Workspace. The same
contract later supports public onboarding from an Eva UI without changing the webhook identity
model.

## Webhook Security and Ingestion

The API exposes `POST /webhooks/telegram`.

It requires `X-Telegram-Bot-Api-Secret-Token` to exactly match the configured secret. Invalid
requests return an authentication error before parsing user content. Valid Telegram updates are
strictly validated and size bounded.

Supported updates are:

- private text messages
- `/start <pairing-token>`
- `/new`
- callback queries, persisted for Milestone 8

Ordinary inbound messages from unpaired identities are acknowledged without processing. This avoids
turning the endpoint into an identity-oracle or causing Telegram to retry an unauthorized update.
Unsupported update types are also safely acknowledged.

An accepted message becomes a canonical Event:

- `source=TELEGRAM`
- `type=telegram.message.received` or `telegram.callback.received`
- `principal_type=USER` only after the persisted account mapping succeeds
- idempotency key `telegram:update:<update_id>`
- selected numeric provider IDs, reply reference, command metadata, and bounded text in payload

The Event and its `telegram.turn.requested` outbox row are committed together. The outbox
destination is the Telegram-turn topic rather than the Gmail relevance topic. Telegram messages
are direct user intent and must not be classified by the Gmail relevance engine.

## Conversations and Turns

`telegram_conversations` stores:

- UUID, User, Workspace, Telegram account, and Situation
- kind: `GENERAL` or `SITUATION`
- status: `ACTIVE` or `CLOSED`
- provider thread anchor where applicable
- bounded running summary for future compaction
- last activity and timestamps

`conversation_turns` stores:

- UUID and tenant-scoped conversation
- role: `USER` or `ASSISTANT`
- canonical Event ID for user turns
- Notification ID for assistant turns
- bounded text
- sequence number and created timestamp
- processing status and lease metadata for user turns
- sanitized provider metadata and usage for completed assistant generation

Unique Event and Notification references make both directions idempotent. Sequence allocation and
turn claiming are serialized per conversation. Only one worker may generate the next assistant turn
at a time, preventing rapid user messages or Pub/Sub redelivery from producing out-of-order replies.

### Correlation rules

1. A reply to a provider message ID owned by a stored Notification selects that Notification's
   Situation and its active Situation conversation.
2. `/new` closes the active general conversation and creates a new `TELEGRAM_CHAT` Situation.
3. An ordinary non-reply uses the account's most recently active conversation.
4. If none exists, Eva creates a general conversation and `TELEGRAM_CHAT` Situation automatically.
5. A reply reference that is unknown to Eva is treated as an ordinary message; it cannot grant
   Situation access.

Milestone 7 adds `TELEGRAM_CHAT` to `SituationType` and `TELEGRAM_CONVERSATION` to correlation-key
kinds. The existing Gmail resolver remains Gmail-specific; a dedicated conversation resolver owns
Telegram Situation creation and lookup.

## Reactive Conversation Agent

The conversational agent is a separate application boundary from the Milestone 6 investigation
agent. Investigation answers “does this external signal require attention?” Conversation answers the
authenticated user's current message. Both reuse common typed output and Gmail evidence contracts
where useful.

The provider-neutral input contains:

- the current user message
- a bounded recent turn window and optional stored summary
- resolved Situation and linked Goals
- active structured facts and retrieved episodic memories
- notification and investigation context when the turn replies to a proactive alert
- explicit capability and authority limits

Eva's PostgreSQL records are the authoritative conversation state. Each model invocation is
stateless at the provider boundary. This preserves auditable, provider-neutral history and avoids
duplicating turns by mixing SDK sessions with application-managed memory.

The default OpenAI adapter uses the Agents SDK with strict output:

```json
{
  "message": "The recruiter is hiring for a Staff Backend role focused on distributed systems.",
  "reasoning_summary": "Answered from the linked email thread and stored career preferences.",
  "proposed_actions": [],
  "memory_proposals": []
}
```

The response text is user-visible. Reasoning summary is a concise audit rationale, never hidden
chain of thought. Proposals remain inert. The application does not apply memory, Situation, or action
proposals in this milestone.

### Gmail read tools

The conversation agent may use the existing bounded `gmail_read_thread` and `gmail_search` tools:

- For a proactive email Situation, `gmail_read_thread` is pinned to that Situation's Gmail thread.
- In a general conversation, `gmail_read_thread` is unavailable because there is no authoritative
  thread; bounded `gmail_search` remains available for the paired Eva user's active Gmail connector.
- Account, User, Workspace, connector, and thread scope are supplied by application state, never by
  model arguments.
- Email/tool content remains untrusted evidence and cannot grant authority or enable Gmail writes.

The model has fixed turn, tool-call, body, history, and timeout limits. SDK tracing stays disabled by
default so user messages and Gmail evidence are not exported through tracing.

## Notifications and Delivery

`notifications` is the durable user-facing delivery record:

- UUID, User, Workspace, optional Situation, and optional AgentRun
- channel: `TELEGRAM`
- kind: `PROACTIVE` or `REACTIVE`
- urgency
- bounded message text
- stable dedupe key
- status: `PENDING`, `SENDING`, `SENT`, `RETRYABLE_FAILURE`, or `PERMANENT_FAILURE`
- attempt count, next retry, claim ID, and lease expiry
- Telegram chat/message IDs only after delivery
- sanitized failure code and timestamps

For a successful AgentRun, dedupe is based on the logical AgentRun and output schema version. For a
reactive response, dedupe is based on the user turn ID and conversation-agent version. Replayed
transactions therefore return the existing Notification.

The delivery worker claims a Notification, loads the currently active scoped Telegram account, and
calls `sendMessage`. Provider calls occur outside the transaction. Success persists the returned
message ID, which becomes the deterministic correlation anchor for future replies.

Transient Telegram transport, quota, timeout, and server failures are retried with bounded
exponential backoff. Invalid chat, revoked bot/account, malformed content, and exhausted attempts are
terminal. A stale claimant cannot overwrite a newer result. Pub/Sub redelivery of a sent or terminal
Notification is acknowledged without sending again.

Telegram cannot provide a universal exactly-once guarantee if a timeout occurs after accepting a
message but before returning its message ID. The stable Notification record, leases, reconciliation
metadata, and conservative retry policy make this ambiguity inspectable.

## Outbox and Pub/Sub

Extend the typed outbound envelope union with:

- `telegram.turn.requested` -> `eva-telegram-turns`
- `notification.delivery.requested` -> `eva-telegram-delivery`

The existing always-running outbox relay publishes both. The worker pool gains two sibling loops:

1. Telegram conversation consumer
2. Telegram delivery consumer

The deployment therefore has six logical loops in one worker-pool instance: Gmail ingestion,
outbox relay, relevance, investigation agent, Telegram conversation, and Telegram delivery. This
remains one Cloud Run worker-pool resource and one shared modular-monolith image.

Separate topics provide independent retry and scaling boundaries for expensive model work and
lightweight provider delivery. Pub/Sub payloads contain identifiers only; workers reload authoritative
tenant-scoped state from PostgreSQL.

## Configuration and Secrets

Milestone 7 adds configuration equivalent to:

```dotenv
EVA_TELEGRAM_ENABLED=false
EVA_TELEGRAM_BOT_USERNAME=
EVA_TELEGRAM_BOT_TOKEN=
EVA_TELEGRAM_WEBHOOK_SECRET=
EVA_TELEGRAM_TURN_TOPIC_ID=eva-telegram-turns
EVA_TELEGRAM_TURN_SUBSCRIPTION_ID=eva-telegram-turns-local
EVA_TELEGRAM_DELIVERY_TOPIC_ID=eva-telegram-delivery
EVA_TELEGRAM_DELIVERY_SUBSCRIPTION_ID=eva-telegram-delivery-local
EVA_TELEGRAM_PAIRING_TTL_SECONDS=900
EVA_TELEGRAM_PULL_TIMEOUT_SECONDS=30
EVA_TELEGRAM_LEASE_SECONDS=300
EVA_TELEGRAM_MAX_ATTEMPTS=6
EVA_TELEGRAM_MESSAGE_MAX_CHARS=4000
EVA_CONVERSATION_MODEL=gpt-5.6-sol
EVA_CONVERSATION_REASONING_EFFORT=medium
EVA_CONVERSATION_AGENT_VERSION=conversation-v1
EVA_CONVERSATION_PROMPT_VERSION=conversation-prompt-v1
EVA_CONVERSATION_HISTORY_TURN_LIMIT=20
EVA_CONVERSATION_HISTORY_MAX_CHARS=24000
EVA_CONVERSATION_MAX_TURNS=6
EVA_CONVERSATION_MAX_TOOL_CALLS=4
```

Production stores the bot token and webhook secret in Secret Manager. The API service account gets
access only to the webhook secret in addition to its database secret. The worker uses its existing
Secret Manager access for Gmail connector grants and OpenAI, and receives the bot token; it never
receives the webhook secret. Neither Telegram value is placed in Terraform state, committed
environment files, logs, Pub/Sub messages, or database rows.

Terraform creates topics and subscriptions unconditionally. A separate production activation flag
adds the secret references, API environment, and the two worker loops only after real secret
versions exist. It does not call Telegram's `setWebhook`, because doing so would put the bot token
into Terraform process output/state and couple provider registration to every infrastructure apply.
The operator performs that idempotent registration once with the CLI after deployment.

## CLI and Operations

```text
eva telegram pairing create
eva telegram account list
eva telegram account revoke
eva telegram webhook set
eva telegram webhook status
eva telegram turn pull
eva telegram delivery pull
eva notification list
eva notification show
eva notification retry
```

All scoped commands require explicit User and Workspace IDs. Pairing tokens and bot secrets are
never included in logs. Notification inspection omits message text by default unless an explicit
content flag is supplied.

Initial personal setup requires the user to create a bot with Telegram's BotFather, store its token
in Secret Manager, deploy, create a pairing link, open that link once, and run the webhook setup
command. Public self-service onboarding can later invoke the same pairing service from Eva's UI.

## Failure Handling

- Duplicate Telegram update: return the existing Event and do not schedule a second turn.
- Webhook process stops after commit: outbox relay publishes later.
- Turn worker stops during model call: lease expiry permits recovery; successful completion is
  guarded by claim identity.
- Conversation message arrives twice: unique Event reference returns the existing user turn.
- Assistant result transaction replays: unique user-turn/version key returns the existing
  Notification.
- Delivery worker stops after claim: lease expiry permits retry.
- Bot or account is revoked: mark delivery terminal and keep the Notification inspectable.
- Gmail authorization is unavailable: the agent may answer without Gmail when possible or return a
  safe reconnect explanation; it never broadens identity scope.
- Rapid messages: serialize per conversation and process in sequence order.

## Observability and Privacy

Structured logs contain only IDs, event/update type, scoped resource identifiers, attempts,
latencies, model metadata, result status, and sanitized error codes. Logs never contain Telegram
message text, conversation history, Gmail evidence/search queries, provider responses, pairing
tokens, bot credentials, webhook secrets, prompts, or hidden reasoning.

Useful metrics include webhook validation failures, accepted/ignored updates, turn queue depth,
conversation latency, model/tool use, notification queue depth, Telegram delivery latency, retries,
and terminal delivery failures.

Conversation text and Notification content are stored because they are required for history,
delivery, audit, and retry. They remain tenant-scoped user data and must follow future retention and
deletion policy.

## Testing Strategy

- pairing expiry, single use, replay, revocation, and cross-Workspace rejection
- webhook secret validation, constant-time comparison boundary, size limits, and private-chat-only
  behavior
- duplicate updates and canonical Event identity
- reply-to-notification, `/new`, active general conversation, and unknown-reply correlation
- per-conversation sequencing, claims, lease expiry, rapid messages, and stale claimant rejection
- bounded history and Situation-first memory assembly
- Gmail thread availability by Situation and scoped search in general chat
- strict conversational output, tool limits, prompt-injection boundary, and inert proposals
- proactive AgentRun completion and atomic Notification/outbox creation
- outbound delivery dedupe, retries, sent-message correlation, and terminal failures
- worker composition and sibling cancellation/cleanup with six loops
- Terraform secret least privilege, Pub/Sub resources, API environment, and validation
- PostgreSQL integration from webhook to stored response Notification and from AgentRun to Telegram
  delivery request

## Acceptance Criteria

- A paired user can send Eva an ordinary Telegram text at any time and receive a context-aware reply.
- A successful investigation notification is delivered proactively and linked to its Situation.
- Replying to a proactive message restores the exact Notification and Situation context.
- `/new` creates a clean general conversation without losing durable history.
- Webhook handling performs no model or provider call and safely tolerates duplicate delivery.
- Conversation context is bounded, tenant-scoped, Situation-first, and backed by Eva's own durable
  history and memory.
- Gmail access is read-only and cannot cross the authenticated user's connector or Situation scope.
- Every outbound message is represented by one durable, deduplicated Notification.
- No conversation output directly writes Gmail, applies memory, changes a Situation, or executes an
  action.
- The Cloud Run worker pool continuously runs all six loops and recovers from redelivery/restarts.
- Secrets are least-privileged and absent from code, Terraform state, logs, Pub/Sub, and database.
- CI, migrations, PostgreSQL tests, integration tests, and Terraform validation pass.
