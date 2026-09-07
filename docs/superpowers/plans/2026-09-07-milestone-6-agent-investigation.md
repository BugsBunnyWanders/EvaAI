# Milestone 6 Agent Investigation Implementation Plan

**Goal:** Add durable, bounded agent investigations for `INVESTIGATE` Signals with read-only Gmail
tools and strict proposal-only results.

**Architecture:** Relevance transactionally creates one AgentRun and outbox message. The existing
relay publishes to a dedicated agent topic. A fourth loop in the existing worker pool claims the
run, builds scoped context, executes a stateless OpenAI Agents SDK run with two read-only Gmail tools,
and persists a typed result without applying side effects.

**Tech Stack:** Python 3.14, Pydantic 2, SQLAlchemy 2 async, PostgreSQL 17, Alembic, Google Gmail API,
Google Pub/Sub, OpenAI Agents SDK 0.22.x, pytest, Ruff, strict mypy, Terraform, uv.

**Spec:** `docs/superpowers/specs/2026-09-07-milestone-6-agent-investigation-design.md`

## Global Constraints

- Work only on `codex/milestone-6-agent-investigation`.
- Use red-green-refactor for behavior changes.
- Require User and Workspace IDs on every read and write.
- Keep Gmail and OpenAI calls outside database transactions.
- Never log or return email content, search queries, credentials, prompts, raw provider responses,
  hidden reasoning, or raw exceptions.
- Treat email/tool content as untrusted evidence, never user authority.
- Register only bounded read-only Gmail tools.
- Store proposals only; do not apply Situation, memory, notification, or action side effects.
- Do not merge the pull request.

## Task 1 — Agent domain contracts

- [x] Add AgentRun statuses, request envelope, claim, result, proposal, tool-audit, and record types.
- [x] Add strict bounds, enums, timestamps, and content-free typed errors.
- [x] Add provider-neutral `InvestigationAgent` and Gmail investigation gateway protocols.
- [x] Test malformed output, proposal bounds, timestamps, and secret-safe representations.

## Task 2 — AgentRun persistence and migration

- [x] Add the `AgentRun` ORM model with composite tenant foreign keys and idempotency constraint.
- [x] Add Alembic revision `20260907_0007` with status, lease, retry, and audit constraints.
- [x] Implement scoped scheduling, claim, completion, failure, retry, and inspection repository APIs.
- [x] Test cross-workspace rejection, one-run-per-Signal idempotency, stale claims, and lease recovery.

## Task 3 — Transactional investigation scheduling

- [x] Make `PreparedRelevanceCommit` capture the persisted Signal and schedule only `INVESTIGATE`.
- [x] Insert AgentRun and `agent.run.requested` outbox rows in the same relevance transaction.
- [x] Preserve behavior for existing Signals and `IGNORE`, `RECORD`, and `NOTIFY` dispositions.
- [x] Test initial evaluation, explicit reevaluation, backfill, transaction rollback, and replay.

## Task 4 — Typed outbox routing and Pub/Sub

- [x] Generalize outbound envelopes to support Event and AgentRun messages.
- [x] Publish discriminated message attributes without changing existing Event behavior.
- [x] Add the agent topic/subscription and IAM wiring to production Terraform.
- [x] Test mixed outbox batches, poison payload handling, destinations, and Terraform validation.

## Task 5 — Read-only Gmail investigation gateway

- [x] Extend the Gmail client contract and adapter with bounded thread retrieval.
- [x] Implement scoped connector/secret resolution for an AgentRun.
- [x] Add bounded normalized thread and search result contracts without HTML/attachment bytes.
- [x] Test ownership, thread authority, pagination exclusion, unavailable messages, and auth failures.

## Task 6 — OpenAI Agents SDK adapter

- [x] Add and lock the `openai-agents` dependency and its required OpenAI Python SDK 3.x.
- [x] Implement strict `AgentInvestigationResult` output with `gpt-5.6-sol` configuration.
- [x] Wrap the Gmail gateway as two function tools with timeouts and aggregate call limits.
- [x] Add stable injection-resistant instructions and bounded canonical run input.
- [x] Disable SDK tracing by default and extract only safe response/usage metadata.
- [x] Test tool exposure, tool budget, structured result conversion, max turns, and safe failures.

## Task 7 — Agent service and pull worker

- [x] Claim a logical run, load scoped state, build memory context, and execute outside transactions.
- [x] Persist success or classified failure with exponential retry scheduling.
- [x] Implement Pub/Sub ack/nack, poison-message handling, terminal redelivery, and cancellation.
- [x] Test idempotent completion, lease loss, retry exhaustion, and connector authorization failure.

## Task 8 — Runtime composition and CLI

- [x] Add validated agent settings and `.env.example` entries.
- [x] Add agent pull plus list/show/retry operator commands with stable JSON.
- [x] Add the agent loop to `eva worker run` and verify sibling cancellation/cleanup.
- [x] Test builders, parser routing, missing configuration, and cleanup behavior.

## Task 9 — Documentation and verification

- [x] Add `docs/agent-operator.md` with lifecycle, privacy, retries, and smoke testing.
- [x] Update README architecture/status and deployment documentation.
- [x] Run Ruff formatting/linting, strict mypy, unit tests, PostgreSQL tests, migrations, and
  Terraform validation.
- [x] Review the diff for secrets, prompt/email logging, direct side effects, and unrelated changes.
- [ ] Commit, push `codex/milestone-6-agent-investigation`, and open a PR to `main`.
