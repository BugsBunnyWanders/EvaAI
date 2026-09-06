# Milestone 5 Memory and Context Implementation Plan

**Goal:** Add explicit, provenance-rich structured and episodic memory with pgvector retrieval and
a bounded, Situation-first Context Builder.

**Architecture:** Keep memory writes behind deterministic policy and scoped repositories. Structured
facts supersede in one transaction. Episodic summaries are embedded outside transactions and stored
atomically with Goal links. Context building loads exact operational state first, then bounded facts
and hybrid-ranked episodes. No model or external event writes memory directly.

**Tech Stack:** Python 3.14, Pydantic 2, SQLAlchemy 2 async, PostgreSQL 17, pgvector, Alembic,
OpenAI Python SDK 2.x embeddings API, argparse, pytest, Ruff, strict mypy, uv.

**Spec:** `docs/superpowers/specs/2026-09-06-milestone-5-memory-context-design.md`

## Global Constraints

- Work only on `codex/milestone-5-memory-context`.
- Use red-green-refactor for behavior changes.
- Require User and Workspace IDs on every read and write.
- Keep OpenAI calls outside database transactions.
- Never log or return embedding vectors, secrets, provider responses, or raw exceptions.
- Never infer user intent or authorization from Gmail/external content.
- Preserve fact and episode audit history; retraction is not deletion.
- Do not merge the pull request.

## Task 1 — Domain contracts and safety policy

- [x] Add strict memory enums, drafts, records, proposal types, context shapes, and normalization.
- [x] Add content-free typed errors.
- [x] Implement deterministic credential/authorization namespace rejection.
- [x] Test bounds, timestamps, provenance, normalization, and policy decisions.

## Task 2 — PostgreSQL persistence

- [x] Add the `pgvector` Python dependency.
- [x] Add MemoryFact, EpisodicMemory, and EpisodicMemoryGoal ORM models.
- [x] Add Alembic revision `20260906_0006` with scoped foreign keys and indexes.
- [x] Test upgrade shape, scope enforcement, active-slot uniqueness, and vector storage.

## Task 3 — Structured memory service

- [x] Add scoped repository reads and writes.
- [x] Implement atomic fact creation/supersession and history-preserving retraction.
- [x] Add idempotent fact replay behavior.
- [x] Test Workspace/Goal/Situation slots, concurrency boundaries, and history.

## Task 4 — Episodic embedding and storage

- [x] Add provider-neutral embedding protocol and validation.
- [x] Add OpenAI embeddings adapter with explicit model and dimensions.
- [x] Embed before transaction, then store episode and Goal links atomically.
- [x] Add Workspace-scoped idempotency and retraction.
- [x] Test invalid vectors, safe failures, replay, and cross-scope Goal rejection.

## Task 5 — Semantic retrieval and hybrid ranking

- [x] Add exact cosine candidate retrieval with strict tenant/model/dimension filters.
- [x] Implement deterministic semantic, importance, recency, entity, and Goal reranking.
- [x] Test rank components, stable ties, candidate limits, and workspace isolation.

## Task 6 — Situation-first Context Builder

- [x] Load the scoped Situation snapshot and linked active Goals.
- [x] Retrieve active valid facts by Situation, Goal, then Workspace scope.
- [x] Build bounded embedding query text and retrieve/rerank episodes.
- [x] Emit strict bounded context with provenance and canonical digest.
- [x] Test priority, bounds, no-episode provider skipping, and safe degradation.

## Task 7 — Runtime composition and CLI

- [x] Add embedding/context configuration with hard bounds.
- [x] Add memory fact and episode create/list/show/retract/search commands.
- [x] Add context build command and stable JSON serialization.
- [x] Test parser routing, required scope, content-free errors, and secret-safe output.

## Task 8 — Documentation and verification

- [x] Add `.env.example` settings and Make wrappers for common inspection commands.
- [x] Add `docs/memory-operator.md` with safety, lifecycle, retrieval, and smoke testing.
- [x] Update README architecture and milestone status.
- [x] Run Ruff formatting/linting, strict mypy, unit tests, PostgreSQL tests, and migrations.
- [x] Review the diff for secrets and unrelated changes.
- [ ] Commit, push `codex/milestone-5-memory-context`, and open a PR to `main`.
