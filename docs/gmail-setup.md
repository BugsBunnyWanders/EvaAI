# Gmail ingestion operator guide

This guide prepares Eva's local Gmail ingestion path for `saswatray2505@gmail.com`. Gmail access is read-only. Eva stores authorized-user credentials in Google Secret Manager, stores only the secret resource name in PostgreSQL, and receives Gmail notifications through a pull subscription. There is no public webhook.

Do not print, inspect in a terminal, commit, or transmit the downloaded OAuth JSON or the authorized-user credentials. Commands in this guide use project `evaai-507018`, topic `eva-gmail-notifications`, and subscription `eva-gmail-ingestion-local`.

## Milestone smoke scope and deployment gate

Task 10 added the CLI and documentation. Task 11 performed the local GCP/OAuth smoke after the required manual checkpoints.

The local smoke used an External app in **Testing** only as an explicit exception after publishing was blocked on public branding, privacy, and terms URLs. That refresh authorization may expire after seven days and require reconnection. This exception proves only the local smoke path; it is not production-readiness evidence and does not change the deployment requirement. Before deployment, configure External + **In production**, publish the required public URLs, and rotate/reconnect the stored credential. Google Auth Platform and Desktop OAuth client creation remain manual Console steps.

## Task 11: idempotent command-line GCP setup

Authenticate the `gcloud` CLI to the intended operator account first. These commands enable only the required APIs, create the topic and pull subscription when absent, and grant Gmail's push service identity publisher access on this topic only.

```bash
gcloud services enable gmail.googleapis.com pubsub.googleapis.com secretmanager.googleapis.com --project=evaai-507018

gcloud pubsub topics describe eva-gmail-notifications --project=evaai-507018 >/dev/null 2>&1 || \
  gcloud pubsub topics create eva-gmail-notifications --project=evaai-507018

gcloud pubsub topics add-iam-policy-binding eva-gmail-notifications --project=evaai-507018 --member=serviceAccount:gmail-api-push@system.gserviceaccount.com --role=roles/pubsub.publisher

gcloud pubsub subscriptions describe eva-gmail-ingestion-local --project=evaai-507018 >/dev/null 2>&1 || \
  gcloud pubsub subscriptions create eva-gmail-ingestion-local --project=evaai-507018 --topic=eva-gmail-notifications

gcloud auth application-default login
```

Verify the resulting resources without changing them:

```bash
gcloud pubsub topics get-iam-policy eva-gmail-notifications --project=evaai-507018
gcloud pubsub subscriptions describe eva-gmail-ingestion-local --project=evaai-507018
gcloud services list --enabled --project=evaai-507018 --filter='name:(gmail.googleapis.com OR pubsub.googleapis.com OR secretmanager.googleapis.com)'
```

The topic project must be the same developer project used by Gmail `users.watch`.

## Manual checkpoint: Google Auth Platform and Desktop OAuth

Complete these steps in Google Cloud Console. They are intentionally not automated by Eva:

1. Open Google Auth Platform for project `evaai-507018`.
2. Configure the audience as **External**.
3. Set publishing status to **In production**. Leaving an External app in Testing can cause short-lived refresh authorization unsuitable for the local worker.
4. Declare only `https://www.googleapis.com/auth/gmail.readonly`. Do not add send, compose, modify, label, archive, delete, or full-mail scopes.
5. Create an OAuth client with application type **Desktop app**.
6. Download the client JSON without opening or printing it, then place it at `.secrets/google-oauth-client.json`.
7. Confirm the file is protected before continuing:

   ```bash
   test -f .secrets/google-oauth-client.json
   git check-ignore .secrets/google-oauth-client.json
   ```

During `eva gmail connect`, Google opens a localhost loopback consent flow. Select `saswatray2505@gmail.com`, confirm the single read-only scope, and pass the unverified-app warning for this personal-use app. Stop if the account or requested scope differs.

Testing mode was permitted only for the completed local Milestone 2 smoke. A Testing-mode token may expire in seven days. Do not treat that exception as permission to deploy or as satisfying External + In production.

## Local configuration and migration

Create the local environment only when it does not already exist, then retain the approved
identifiers. If `.env` already exists, merge the missing keys from `.env.example` instead of
overwriting it:

```bash
test -e .env || cp .env.example .env
make setup
make db-up
make migrate
```

Required non-secret values are:

```dotenv
EVA_PUBSUB_PROJECT_ID=evaai-507018
EVA_GMAIL_TOPIC_ID=eva-gmail-notifications
EVA_GMAIL_SUBSCRIPTION_ID=eva-gmail-ingestion-local
EVA_GMAIL_ACCOUNT=saswatray2505@gmail.com
EVA_GMAIL_OAUTH_CLIENT_FILE=.secrets/google-oauth-client.json
EVA_GMAIL_REQUEST_TIMEOUT_SECONDS=30
EVA_GMAIL_RETRY_ATTEMPTS=3
EVA_GMAIL_RETRY_INITIAL_BACKOFF_SECONDS=0.5
EVA_GMAIL_RETRY_MAX_BACKOFF_SECONDS=8
EVA_GMAIL_RETRY_JITTER_RATIO=0.2
```

Do not place OAuth codes, refresh tokens, access tokens, or client JSON contents in `.env`.
The request timeout configures the blocking Gmail HTTP socket itself. Total attempts include the initial request. Transient request timeouts, 429/rate-limit responses (including Gmail's reason-coded rate-limit 403s), server errors, and network/transport failures retry with bounded exponential backoff and jitter. Revoked authorization and expired history cursors use their dedicated recovery paths instead of consuming the transient retry budget.

## Create the explicit local scope

Create exactly one local User and its owned Workspace:

```bash
uv run eva scope create --display-name "Saswat Ray" --workspace-name personal
```

The command prints two UUID-only lines: User first, Workspace second. Record them locally without posting them to shared logs:

```bash
export EVA_USER_ID=USER_UUID_FROM_LINE_1
export EVA_WORKSPACE_ID=WORKSPACE_UUID_FROM_LINE_2
```

## Connect Gmail

After the manual OAuth client checkpoint:

```bash
make gmail-connect
```

The Make target receives the exported UUIDs as quoted shell data. The equivalent direct command is
`uv run eva gmail connect --user-id "$EVA_USER_ID" --workspace-id "$EVA_WORKSPACE_ID"`.

The command verifies the explicit persisted ownership pair before constructing Google clients or opening a browser. It samples a local timestamp, verifies the authorized Gmail profile against `EVA_GMAIL_ACCOUNT`, and durably stores that profile's current history cursor and timestamp before creating the inbox watch with topic `projects/evaai-507018/topics/eva-gmail-notifications`. That lower boundary excludes pre-profile history while treating mail in the short profile-to-watch window as post-connection. The command stores refresh credential material in Secret Manager without the current access token or expiry, activates the watch without replacing the lower cursor, and prints only the ConnectorAccount UUID.

```bash
export EVA_GMAIL_CONNECTOR_ID=CONNECTOR_UUID
```

Re-running connect for the same Workspace and Gmail identity refreshes authorization/watch state without changing the original connection boundary or durable history cursor. The command exits zero only after the connector is `ACTIVE`.

## Run ingestion and maintenance

Start the continuous pull worker in a dedicated terminal:

```bash
make gmail-pull
```

The Pub/Sub pull timeout gives the worker a bounded maintenance wake-up. Stop with Ctrl-C and allow cleanup to finish.

Run one deterministic stored-cursor synchronization attempt when replay verification or recovery diagnosis requires it:

```bash
make gmail-sync
```

The one-shot sync exits zero only when its result is `SYNCED`; busy, connecting, unknown-account, and reauthorization-required results exit nonzero with a content-free error.

Run one persisted due-maintenance pass and exit:

```bash
make gmail-maintain
```

`gmail maintain` renews due watches without replacing the durable cursor and performs safety synchronization only after the configured notification-silence interval. It exits nonzero when the maintenance summary reports any failed connector.

## Inspect connector and Event state

Inspect operational identifiers and cursor state without selecting credential or email-content fields:

```bash
docker compose exec postgres psql -U eva -d eva -c \
  "SELECT c.id, c.status, c.secret_reference, s.history_id, s.watch_expiration, s.last_notification_at, s.last_successful_sync_at, s.next_watch_renewal_at, s.next_safety_sync_at FROM connector_accounts c JOIN gmail_sync_states s ON s.connector_account_id = c.id WHERE c.id = '$EVA_GMAIL_CONNECTOR_ID';"

docker compose exec postgres psql -U eva -d eva -c \
  "SELECT id, event_type, external_id, idempotency_key, occurred_at, correlation_keys FROM events WHERE principal_id = '$EVA_GMAIL_CONNECTOR_ID' ORDER BY occurred_at DESC;"

docker compose exec postgres psql -U eva -d eva -c \
  "SELECT e.external_id, count(DISTINCT e.id) AS events, count(DISTINCT o.id) AS outbox_rows FROM events e LEFT JOIN outbox_messages o ON o.event_id = e.id WHERE e.principal_id = '$EVA_GMAIL_CONNECTOR_ID' GROUP BY e.external_id ORDER BY e.external_id;"
```

For the Task 11 smoke test, send messages only after connection: one plain-text message, one HTML message, one message with a small attachment, and a Promotions/Social/Updates-category message when available. Expect one `email.received` Event and one Outbox row per Gmail message. Attachment metadata is retained; attachment binaries are not downloaded.

## Recovery procedures

- **Worker restart:** restart `make gmail-pull`. The cursor and all due timestamps are persisted; no in-process sleep owns correctness.
- **Suspected missed notification:** run `make gmail-maintain`, then `make gmail-sync`. Event idempotency makes replay safe.
- **Expired Gmail history cursor:** a stored-cursor sync first establishes a replacement watch, then performs the bounded inbox scan from `connected_at`. After every recovered Event is durable, it atomically publishes the replacement cursor, watch expiration, and renewal/safety schedules. Notifications racing the scan remain retryable from the old cursor until that completion commits.
- **`REAUTHORIZATION_REQUIRED`:** confirm the Desktop client file is still a regular ignored file, then rerun `gmail connect` with the same User and Workspace IDs and complete consent for the configured account.
- **Retryable `ERROR`:** check ADC, API enablement, topic IAM, network access, and Secret Manager access; then rerun connect or the failed one-pass command. Do not create another ownership scope to bypass an error.

## Troubleshooting

- `eva: command failed` is intentionally content-free. Confirm all required environment values are present, migrations are at head, PostgreSQL is reachable, and UUIDs were copied exactly.
- If connect fails before a browser opens, verify `EVA_PUBSUB_PROJECT_ID`, `EVA_GMAIL_ACCOUNT`, `EVA_GMAIL_TOPIC_ID`, the User/Workspace pair, and that `.secrets/google-oauth-client.json` exists and is a regular file. Do not print the file.
- If Gmail rejects the watch, verify the fully qualified topic is in `evaai-507018` and the Gmail push service account has `roles/pubsub.publisher` on that topic.
- If transient Gmail calls exhaust their configured attempts, check network/DNS reachability and quota before increasing the socket timeout or retry budget. Keep the attempt count and maximum backoff bounded so a request cannot silently outlive the synchronization lease.
- If pulls time out with no messages, confirm the subscription targets the approved topic. An empty long-poll deadline is normal.
- If authorization expires within seven days, the local exception is still using Testing mode. Reconnect for local smoke continuity if necessary, but treat External + **In production**, required public URLs, and credential rotation as unresolved deployment gates.
- Never paste provider responses, OAuth JSON, tokens, email subjects/bodies, full address lists, or the database URL into logs or issue reports.

## Teardown — documentation only, do not run during milestone work

The following commands are destructive and are recorded only for a deliberate future teardown. Task 10 and Task 11 do **not** execute them.

```bash
gcloud pubsub subscriptions delete eva-gmail-ingestion-local --project=evaai-507018
gcloud pubsub topics remove-iam-policy-binding eva-gmail-notifications --project=evaai-507018 --member=serviceAccount:gmail-api-push@system.gserviceaccount.com --role=roles/pubsub.publisher
gcloud pubsub topics delete eva-gmail-notifications --project=evaai-507018
gcloud secrets delete "eva-gmail-oauth-$EVA_GMAIL_CONNECTOR_ID" --project=evaai-507018
```

Separately revoke Eva's account access from the Google Account security page and delete the Desktop OAuth client in Google Cloud Console only when permanent teardown is intended.
