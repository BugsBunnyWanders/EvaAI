# Eva AI

Eva is a proactive, event-driven personal AI operator. The repository contains the application foundation, durable Event backbone, local Gmail ingestion worker, and durable Goal and Situation domain.

## Requirements

- Python 3.14 (managed automatically by `uv`)
- uv 0.12+
- Docker with Docker Compose

## Local setup

Copy `.env.example` to `.env`, then run:

```bash
make setup
make db-up
make migrate
make run
```

The API listens on `http://127.0.0.1:8000`.

```bash
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

Both endpoints return HTTP 200 when the application and database are ready.

## Public website

Eva's dependency-free public website lives in `site/`. It provides the product homepage and the stable policy URLs used by Google OAuth:

- `https://evaatyourservice.com/`
- `https://evaatyourservice.com/privacy/`
- `https://evaatyourservice.com/terms/`

Preview the site locally without starting Eva's backend:

```bash
uv run python -m http.server 8080 --directory site
```

Open `http://127.0.0.1:8080`. The site contains no external scripts, analytics, cookies, or build-time dependencies. Its structural and policy contract tests run as part of `make verify`.

### GitHub Pages deployment

The `Deploy public site` GitHub Actions workflow publishes `site/` after a site-related change reaches `main`. In the repository's **Settings → Pages**, select **GitHub Actions** as the build source if it is not selected automatically. Configure the custom domain as `evaatyourservice.com` before changing DNS; GitHub recommends setting the custom domain first to reduce takeover risk.

After the custom domain is saved in GitHub, add these records in Hostinger's DNS Zone Editor:

| Type | Name | Target |
| --- | --- | --- |
| A | `@` | `185.199.108.153` |
| A | `@` | `185.199.109.153` |
| A | `@` | `185.199.110.153` |
| A | `@` | `185.199.111.153` |
| CNAME | `www` | `bugsbunnywanders.github.io` |

Do not remove or replace existing MX or TXT records used for email, ownership, or other services. Wait for GitHub's DNS check to pass, then enable **Enforce HTTPS**. DNS and certificate issuance can take time to propagate.

### Google OAuth production handoff

Once all three HTTPS URLs resolve publicly:

1. Verify `evaatyourservice.com` in [Google Search Console](https://search.google.com/search-console/about), normally with the DNS TXT method.
2. In Google Auth Platform → Branding, set the application homepage to `https://evaatyourservice.com/`, the privacy policy to `https://evaatyourservice.com/privacy/`, and the terms link to `https://evaatyourservice.com/terms/`.
3. Add `evaatyourservice.com` as an authorized domain and save the branding configuration.
4. In Audience, publish the OAuth app to **In production**. Personal use can remain unverified, but Google may show an unverified-app warning.
5. Reauthorize the Gmail connector after production publishing so the local Testing refresh token is replaced with a production authorization.

The Pages merge does not itself change Hostinger DNS, verify Search Console, publish the OAuth app, or rotate Gmail authorization. Those remain deliberate operator steps.

## Gmail ingestion

Milestone 2 adds Desktop OAuth bootstrap, Gmail watch/history synchronization, a Google Pub/Sub pull subscriber, persisted watch maintenance, and expired-cursor recovery. Setup requires manual Google Auth Platform configuration and local GCP resources; follow the [Gmail ingestion operator guide](docs/gmail-setup.md) before running the worker.

The local command surface is:

```bash
uv run eva scope create --display-name "Saswat Ray" --workspace-name personal
uv run eva gmail connect --user-id USER_UUID --workspace-id WORKSPACE_UUID
uv run eva gmail sync --connector-id CONNECTOR_UUID
uv run eva gmail pull
uv run eva gmail maintain
```

The four Gmail Make wrappers consume IDs from exported environment variables as literal shell
data. Create the scope with the direct CLI command above, export the UUIDs it prints, then use:

```bash
export EVA_USER_ID=USER_UUID
export EVA_WORKSPACE_ID=WORKSPACE_UUID
export EVA_GMAIL_CONNECTOR_ID=CONNECTOR_UUID
make gmail-connect
make gmail-sync
make gmail-pull
make gmail-maintain
```

There is no public Gmail webhook or HTTP ingestion endpoint. Gmail notifications are consumed from the configured pull subscription.

## Verification

With PostgreSQL running:

```bash
make verify
```

This runs Ruff formatting and lint checks, strict mypy checking, unit tests, PostgreSQL integration tests, and migration verification.

## Database migrations

Create revisions deliberately and review them before applying:

```bash
uv run alembic revision --autogenerate -m "describe change"
uv run alembic upgrade head
```

Migrations never run automatically during API startup.

## Architecture

The product architecture is in `spec/2026-08-29-proactive-personal-ai-agent-design.md`. Milestone designs and implementation plans are stored under `docs/superpowers/`.

### Milestone 1: Event backbone

Milestone 1 adds a durable event backbone with this flow:

```text
NewEvent -> PostgreSQL transaction [Event + EventProcessing + OutboxMessage]
         -> OutboxRelay claim -> Publisher acknowledgement
         -> EventProcessor claim -> EventHandler -> HANDLED
```

Local tests use the in-memory publisher. When the Google Pub/Sub adapter is selected, it
uses Application Default Credentials and requires `EVA_PUBSUB_PROJECT_ID`; this milestone
does not create any GCP resources. Publication is at-least-once, so event handling remains
idempotent across redelivery.

Milestone 1's Event and Outbox reliability boundary is reused by the Milestone 2 Gmail subscriber. Telegram behavior remains deferred.

### Milestone 3: Goals and Situations

Milestone 3 adds scoped Goal and Situation persistence and services, lifecycle enforcement,
optimistic Situation snapshots, explicit Event/Goal relationships, and deterministic
Gmail-thread correlation. The local CLI can create, list, show, and update Goals and can list
and inspect Situations with stable JSON output.

Gmail ingestion does not automatically create Situations yet. Milestone 4 will evaluate Event
relevance and invoke the resolver only for email that warrants operational attention. See the
[Goal and Situation operator guide](docs/goal-situation-operator.md) for the mental model,
command examples, lifecycle rules, and troubleshooting.
