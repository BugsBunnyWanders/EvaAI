# Milestone 7 Telegram Conversation Implementation Plan

**Goal:** Add authenticated, durable, user-specific Telegram conversation so Eva can initiate
Situation-linked alerts and the paired user can initiate or continue real-time text conversations.

**Architecture:** The API verifies and durably ingests Telegram webhooks without external calls. The
existing outbox relay routes inbound turns and outbound Notifications to dedicated Pub/Sub topics.
Two new loops in the existing worker pool run the conversation agent and Telegram delivery. Every
lookup and mutation is scoped to the Telegram account's User and Workspace.

**Tech Stack:** Python 3.14, FastAPI, Pydantic 2, SQLAlchemy 2 async, PostgreSQL 17, Alembic,
Telegram Bot API, Google Pub/Sub and Secret Manager, OpenAI Agents SDK 0.22.x, pytest, Ruff, strict
mypy, Terraform, uv.

**Spec:** `docs/superpowers/specs/2026-09-07-milestone-7-telegram-conversation-design.md`

**Status:** Complete.

## Global Constraints

- Work only on `codex/milestone-7-telegram-notifications`.
- Use red-green-refactor for behavior changes.
- Resolve identity from the persisted numeric Telegram user ID and private chat ID.
- Require User and Workspace IDs on every application and repository read/write.
- Keep OpenAI, Gmail, Telegram, Pub/Sub, and Secret Manager calls outside database transactions.
- Never log message text, history, email/tool content, search queries, credentials, prompts, raw
  provider responses, pairing tokens, hidden reasoning, or raw exceptions.
- Treat Telegram text and Gmail evidence as untrusted content, but authenticated paired Telegram
  messages as user intent. User intent still does not bypass policy or grant new capabilities.
- Register read-only Gmail tools only; do not draft, send, label, delete, or modify Gmail.
- Keep all model proposals inert in Milestone 7.
- Do not merge the pull request.

## Task 1 — Telegram and conversation domain contracts

- [x] Add strict enums and records for Telegram accounts, pairing codes, conversations, turns,
  notifications, claims, provider results, and retry outcomes.
- [x] Add typed inbound-update, conversation-turn, and delivery-request envelopes with bounded text
  and schema versions.
- [x] Add provider-neutral Telegram gateway and ConversationAgent protocols.
- [x] Extend Situation types with `TELEGRAM_CHAT` and `TELEGRAM_CONVERSATION` without widening the
  existing Gmail resolver.
- [x] Test validation, bounds, timezone requirements, secret-safe representations, and envelope
  discrimination.

## Task 2 — Persistence schema and migration

- [x] Add tenant-scoped ORM models for TelegramAccount, TelegramPairingCode,
  TelegramConversation, ConversationTurn, and Notification.
- [x] Add Alembic revision `20260907_0008` with composite User/Workspace foreign keys, unique
  provider/idempotency anchors, lifecycle checks, leases, and indexes.
- [x] Extend Situation database constraints for Telegram chat correlations.
- [x] Add model exports and schema tests for cascade/restrict behavior and cross-tenant rejection.
- [x] Verify full upgrade from base through the new head and downgrade back one revision.

## Task 3 — Pairing and account lifecycle

- [x] Implement high-entropy pairing-token creation with digest-only persistence, expiry, and
  single-use atomic consumption.
- [x] Implement numeric Telegram user/chat mapping, reactivation, listing, and revocation.
- [x] Permit only private chats and bind both provider identifiers to one User and Workspace.
- [x] Add `eva telegram pairing create`, `account list`, and `account revoke` commands.
- [x] Test expiry, replay, concurrent consumption, display-name changes, revocation, and
  cross-Workspace attempts.

## Task 4 — Verified Telegram webhook

- [x] Add `POST /webhooks/telegram` with constant-time secret-header verification, request-size
  bounds, and strict update parsing.
- [x] Handle `/start <token>`, `/new`, ordinary private text, reply references, and callback-query
  persistence; safely acknowledge unsupported or unpaired updates.
- [x] Normalize accepted messages to canonical `telegram.message.received` Events with paired USER
  principal and `telegram:update:<update_id>` idempotency.
- [x] Persist the Event and Telegram-turn outbox message atomically, then return HTTP 200 without
  calling external providers.
- [x] Test invalid/missing secrets, duplicate updates, group rejection, unpaired identities,
  malformed payloads, and commit failures.

## Task 5 — Conversation and Situation resolution

- [x] Implement deterministic reply-to-Notification correlation using the stored provider message
  ID and the same User/Workspace.
- [x] Implement active-conversation lookup, automatic general conversation creation, and `/new`
  creation of a fresh `TELEGRAM_CHAT` Situation.
- [x] Persist one idempotent USER turn per canonical Event and serialize sequence allocation per
  conversation.
- [x] Add tenant-scoped claim, lease expiry, stale claimant, success, failure, and retry operations.
- [x] Test proactive replies, general chat, unknown reply IDs, rapid messages, replay, and ordering.

## Task 6 — Proactive Notification scheduling

- [x] Extend AgentRun completion so a successful result containing a notification atomically creates
  one deduplicated proactive Notification and delivery outbox row.
- [x] Leave successful `NO_ACTION` or null-notification results without a Notification.
- [x] Preserve AgentRun claim protection, provider metadata, and existing retry behavior.
- [x] Add Notification repository list/show/retry APIs with explicit scope and content-redaction
  defaults.
- [x] Test commit rollback, completion replay, dedupe by logical AgentRun/version, and tenant scope.

## Task 7 — Conversation context and Gmail-read scope

- [x] Build a bounded conversation context from recent turns, optional summary, Situation, linked
  Goals, structured facts, episodic memory, and proactive investigation context.
- [x] Reuse the MemoryContextBuilder with the resolved conversation Situation.
- [x] Reuse bounded Gmail search for the paired user's active connector.
- [x] Enable pinned Gmail-thread reading only for an email Situation; keep it unavailable for a
  general Telegram Situation.
- [x] Test history ordering/truncation, memory isolation, connector selection, email thread scope,
  absent/revoked Gmail, and prompt-injection boundaries.

## Task 8 — OpenAI conversational agent

- [x] Add strict conversational output containing user-visible message, concise audit rationale,
  inert action proposals, and inert memory proposals.
- [x] Implement a stateless OpenAI Agents SDK adapter using Eva-managed bounded history.
- [x] Register only the scoped Gmail read tools available for the resolved Situation.
- [x] Add stable instructions distinguishing authenticated user intent from untrusted quoted email
  content while retaining policy/authority limits.
- [x] Disable SDK tracing by default and extract only sanitized response/usage/tool metadata.
- [x] Test strict output conversion, tool availability, tool-call/turn budgets, timeouts, and safe
  provider failures.

## Task 9 — Conversation service and pull worker

- [x] Consume `telegram.turn.requested`, claim the USER turn, build context, and invoke the agent
  outside database transactions.
- [x] Atomically complete the turn, persist the ASSISTANT turn, create a reactive Notification, and
  enqueue delivery.
- [x] Implement Pub/Sub ack/nack rules, bounded exponential retry, poison-message handling, terminal
  outcomes, cancellation, and redelivery.
- [x] Ensure a completed user turn/version cannot cause a second model call or Notification.
- [x] Test idempotency, ordering, lease loss, retry exhaustion, and sibling failure cleanup.

## Task 10 — Telegram delivery gateway and worker

- [x] Implement a bounded async Telegram Bot API client for `sendMessage`, `setWebhook`,
  `getWebhookInfo`, and required callback acknowledgement.
- [x] Claim a Notification, resolve its current scoped active account, send outside the transaction,
  and persist the provider message ID for future reply correlation.
- [x] Implement retry classification, terminal failures, stale claim protection, and sent-message
  redelivery acknowledgement.
- [x] Add `eva telegram delivery pull` and Notification list/show/retry CLI commands.
- [x] Test request construction, token redaction, timeout-after-success ambiguity metadata, provider
  errors, dedupe, and reply anchor persistence.

## Task 11 — Runtime composition and configuration

- [x] Add validated Telegram and conversation settings plus `.env.example` entries.
- [x] Compose the webhook dependencies in FastAPI lifespan without exposing secrets.
- [x] Add conversation and delivery subscribers as fifth and sixth loops in `eva worker run`.
- [x] Add `eva telegram turn pull`, webhook set/status, and stable JSON output.
- [x] Test disabled/partial configuration, runtime builders, six-loop execution, cancellation, and
  cleanup.

## Task 12 — Terraform and deployment

- [x] Create production Telegram-turn and delivery topics/subscriptions with worker publisher and
  subscriber IAM.
- [x] Add bot-token and webhook-secret Secret Manager references without secret values in Terraform.
- [x] Grant the API service account only webhook-secret access and the worker only bot-token access.
- [x] Wire API and worker environment variables, preserving the migration pause/restore deployment
  strategy.
- [x] Update bootstrap/deployment inputs and validate that plan output contains no secret material.
- [x] Test Terraform validation and configuration assertions.

## Task 13 — Documentation, end-to-end verification, and PR

- [x] Add `docs/telegram-operator.md` covering BotFather, Secret Manager, deploy, webhook registration,
  pairing, testing, retries, revocation, and troubleshooting.
- [x] Update README architecture/status, deployment documentation, and worker-loop count.
- [x] Exercise PostgreSQL flows from Telegram webhook to reactive Notification and from AgentRun to
  proactive Notification.
- [x] Run Ruff formatting/linting, strict mypy, unit tests, PostgreSQL integration tests, all
  migrations, Terraform formatting, and Terraform validation.
- [x] Review the diff for secrets, message/email logging, identity-scope gaps, synchronous webhook
  provider calls, direct Gmail writes, and unrelated changes.
- [x] Commit, push `codex/milestone-7-telegram-notifications`, and open a PR to `main` for the user to
  merge.
