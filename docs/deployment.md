# GCP deployment

Eva uses GitHub Actions as its production delivery system. Pull requests validate application and
Terraform code without receiving Google Cloud credentials. After CI succeeds on `main`, the deploy
workflow builds one immutable image and applies the production Terraform stack.

## Production topology

```text
GitHub Pages                         Google Cloud
evaatyourservice.com                 eva-api (Cloud Run service)
                                     eva-worker (Cloud Run worker pool)
                                       - Gmail pull
                                       - transactional outbox relay
                                       - relevance pull
                                       - agent investigation pull
                                       - Telegram conversation pull
                                       - Telegram delivery pull
                                     eva-migrate (Cloud Run job)
                                     eva-gmail-maintenance (scheduled job)
                                     Cloud SQL PostgreSQL 17 + pgvector
                                     Pub/Sub (Gmail, events, agent runs, Telegram) + Secret Manager
```

The API scales to zero. The worker pool uses one manually scaled instance because pull consumers
must remain active. GitHub pauses that worker during schema migrations and restores the configured
count only after migrations succeed.

## One-time bootstrap

The production workflow cannot create its own identity or remote-state bucket before it can
authenticate. An owner therefore applies the small local-state bootstrap stack exactly once.

1. Install Terraform 1.16.1 or a compatible 1.16 release.
2. Authenticate locally and select the project:

   ```bash
   gcloud auth application-default login
   gcloud config set project evaai-507018
   ```

3. Apply the bootstrap stack:

   ```bash
   cp terraform/bootstrap/terraform.tfvars.example terraform/bootstrap/terraform.tfvars
   terraform -chdir=terraform/bootstrap init
   terraform -chdir=terraform/bootstrap plan -out=bootstrap.tfplan
   terraform -chdir=terraform/bootstrap apply bootstrap.tfplan
   ```

   This enables the required APIs and creates the versioned state bucket, Artifact Registry
   repository, empty OpenAI secret, deployment service account, and repository-restricted Workload
   Identity Federation provider. It does not create Cloud SQL or an always-on worker.

4. Add the OpenAI key as a Secret Manager version. The secret value is never placed in Terraform
   configuration or state:

   ```bash
   read -s "OPENAI_API_KEY?OpenAI API key: "
   printf %s "$OPENAI_API_KEY" | gcloud secrets versions add eva-openai-api-key --data-file=-
   unset OPENAI_API_KEY
   ```

   Before enabling Telegram, also create the two secret containers and add their values as
   described in the [Telegram operator guide](telegram-operator.md). Terraform intentionally
   references existing secret containers so neither value enters Terraform state.

5. Copy the bootstrap outputs into GitHub repository variables:

   ```bash
   gh variable set GCP_PROJECT_ID --body evaai-507018
   gh variable set GCP_REGION --body asia-south1
   gh variable set TF_STATE_BUCKET \
     --body "$(terraform -chdir=terraform/bootstrap output -raw state_bucket)"
   gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER \
     --body "$(terraform -chdir=terraform/bootstrap output -raw workload_identity_provider)"
   gh variable set GCP_DEPLOY_SERVICE_ACCOUNT \
     --body "$(terraform -chdir=terraform/bootstrap output -raw deployer_service_account)"
   gh variable set EVA_WORKER_INSTANCE_COUNT --body 0
   gh variable set EVA_TELEGRAM_ENABLED --body false
   ```

6. In GitHub, create a `production` environment and restrict its deployment branches to `main`.
   Environment reviewers are optional: the merge to protected `main` is already the deployment
   gate. Adding a required reviewer changes apply into an explicit post-merge approval without
   changing the workflow. The Google provider independently rejects tokens whose `ref` is not
   `refs/heads/main`.

Bootstrap state contains IAM resource identifiers but no application secret values. Keep its local
`terraform.tfstate` private and backed up; repository ignore rules prevent accidental commits.

## Merge-to-main release

The existing `CI` workflow runs first. When it succeeds on `main`, `Deploy Eva to GCP`:

1. checks whether deployable application or infrastructure files changed;
2. obtains a short-lived Google credential through GitHub OIDC and Workload Identity Federation;
3. builds and pushes an image tagged with the Git commit;
4. resolves and deploys the image by digest, never by a mutable tag;
5. applies Terraform with the worker count at zero;
6. executes `alembic upgrade head` as the `eva-migrate` Cloud Run job;
7. applies the configured worker count; and
8. calls the API readiness endpoint.

Static-site-only merges remain handled by the Pages workflow and do not redeploy the backend. A
manual run of `Deploy Eva to GCP` always performs a deployment from `main`.

## First production activation

Keep `EVA_WORKER_INSTANCE_COUNT=0` for the first infrastructure deployment. Cloud SQL starts empty,
and the local Gmail connector row is not automatically copied into it. Before starting the worker,
choose one of these operator migrations:

- restore the existing local PostgreSQL data into Cloud SQL; or
- create a fresh production scope and reconnect Gmail while locally connected to Cloud SQL through
  the Cloud SQL Auth Proxy.

The second option preserves Eva's "only email arriving after connection" rule but intentionally
does not copy local test Events, Goals, Situations, or Memory. Once a production Gmail connector is
active, enable the worker and manually rerun the deploy workflow:

```bash
gh variable set EVA_WORKER_INSTANCE_COUNT --body 1
gh workflow run "Deploy Eva to GCP"
```

Do not enable the worker against an empty connector database. Gmail notifications for an unknown
account are acknowledged deliberately and would not be replayed later.

## State, secrets, and deletion protection

- Production state is kept in a private, versioned GCS bucket with state locking.
- GitHub stores only non-secret resource identifiers as repository variables.
- The OpenAI key, Telegram bot token, webhook secret, and runtime database URL are injected from
  Secret Manager according to each runtime's needs.
- The generated database password exists in encrypted Terraform state because Terraform manages
  the Cloud SQL user; never print or download production state unnecessarily.
- Cloud SQL, Cloud Run service, jobs, and worker pool have deletion protection enabled. Removing
  them requires a reviewed configuration change that first disables protection.

The initial defaults are `asia-south1`, single-zone `db-f1-micro`, a 10 GB SSD, API scale-to-zero,
and zero workers until activation. Cloud SQL and every enabled worker instance incur ongoing GCP
charges; review Billing after the first apply and resize before multi-user use.

## Recovery

If a deployment fails before migrations finish, the worker remains at zero. Fix the failure and
rerun the workflow. If migration succeeds but the final worker apply fails, no events are lost:
Gmail and Eva messages remain in their durable Pub/Sub subscriptions. Never bypass the migration
step by manually increasing the worker count.
