# Milestone 5.5 GCP Deployment Implementation Plan

1. Package Eva into a reproducible, non-root Python 3.14 container that includes Alembic.
2. Add one worker command that supervises Gmail, event relay, and relevance loops concurrently.
3. Add a one-time Terraform bootstrap stack for APIs, state, registry, secrets, and keyless GitHub
   federation.
4. Add a production Terraform stack for Cloud SQL, Pub/Sub, identities, Cloud Run, jobs, Scheduler,
   Secret Manager references, and deletion protection.
5. Add PR-only Terraform static checks and trusted merge-to-main deployment after application CI.
6. Pause workers for every migration, restore an operator-controlled count afterward, and perform a
   readiness smoke test.
7. Document bootstrap, first Gmail production activation, cost boundaries, and failure recovery.
8. Verify Python, Terraform, container, and workflow behavior; commit, push, and open a pull request.
