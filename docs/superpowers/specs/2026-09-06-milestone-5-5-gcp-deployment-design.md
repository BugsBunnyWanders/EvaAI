# Milestone 5.5: GCP deployment foundation

## Decision

Deploy Eva from one repository image into two Cloud Run execution units: an HTTP service for the
API and a Worker Pool for continuous pull workloads. Use Cloud Run Jobs for schema migration and
periodic Gmail maintenance. Keep the public site on GitHub Pages.

GitHub Actions is the release controller. Untrusted pull-request jobs receive no cloud credential.
After the normal CI workflow succeeds on `main`, a production-environment job authenticates using
repository-restricted Workload Identity Federation, builds an immutable image, applies Terraform,
runs migrations with consumers paused, restores the desired worker count, and verifies health.

## Runtime boundaries

- `eva-api`: FastAPI health today; OAuth callbacks, Telegram webhooks, and future API later.
- `eva-worker`: one Python supervisor running Gmail pull, outbox relay, and relevance pull. Any loop
  failure cancels the siblings, performs their cleanup, and lets Cloud Run restart the unit.
- `eva-migrate`: one-shot Alembic job executed on every release.
- `eva-gmail-maintenance`: hourly safety sync and watch-renewal job; the continuous Gmail loop also
  performs due maintenance, so leases keep duplicate execution safe.

## Managed state

Terraform owns the Cloud SQL instance, database/user, runtime identities, production Pub/Sub
subscriptions, runtime database secret, Cloud Run resources, and Scheduler job. Declarative import
blocks adopt the two Pub/Sub topics created during local Gmail work. Existing local subscriptions
remain unmanaged so local development continues to work.

A separate one-time bootstrap stack owns the remote-state bucket, Artifact Registry repository,
OpenAI secret container, deployment identity, required APIs, and GitHub federation. This resolves
the authentication and image-registry bootstrapping cycle without using a service-account key.

## Safety boundaries

- no long-lived Google credential in GitHub;
- exact repository and `production` environment OIDC subject required for deploy impersonation;
- no cloud credential in pull-request validation;
- immutable image digests in Terraform;
- worker count forced to zero while migrations run;
- deletion protection for stateful and runtime resources;
- first production worker activation remains explicit until Gmail is connected in Cloud SQL.
