# Telegram operator guide

Milestone 7 gives Eva a private, user-specific Telegram interface. Eva can proactively send an
agent finding, and a paired user can start or continue a free-form conversation at any time.
Telegram does not gain Gmail write authority in this milestone.

## Runtime flow

```text
Proactive
AgentRun succeeds -> Notification + transactional outbox -> eva-telegram-delivery
                  -> delivery worker -> Telegram sendMessage

Reactive
Telegram -> authenticated API webhook -> Event + transactional outbox -> eva-telegram-turns
         -> conversation worker -> scoped Situation + memory + optional Gmail reads
         -> assistant turn + Notification + transactional outbox -> eva-telegram-delivery
         -> delivery worker -> Telegram sendMessage
```

The webhook performs no model, Gmail, Pub/Sub, or Telegram API call. It acknowledges only after the
inbound Event and outbox row commit. The existing relay publishes both Telegram envelope types.
The single Cloud Run worker-pool instance supervises Gmail ingestion, outbox relay, relevance,
investigation agent, Telegram conversation, and Telegram delivery loops.

## 1. Create the bot and secrets

In Telegram, open the verified `@BotFather`, run `/newbot`, choose the display name and username,
and copy the bot token. Do not paste the token into chat, GitHub, Terraform, or a committed file.

Generate an independent webhook secret locally, then create Secret Manager containers and versions:

```bash
gcloud secrets create eva-telegram-bot-token --replication-policy=automatic
gcloud secrets create eva-telegram-webhook-secret --replication-policy=automatic

read -s "TELEGRAM_BOT_TOKEN?Telegram bot token: "
printf %s "$TELEGRAM_BOT_TOKEN" | \
  gcloud secrets versions add eva-telegram-bot-token --data-file=-
unset TELEGRAM_BOT_TOKEN

TELEGRAM_WEBHOOK_SECRET="$(openssl rand -hex 32)"
printf %s "$TELEGRAM_WEBHOOK_SECRET" | \
  gcloud secrets versions add eva-telegram-webhook-secret --data-file=-
unset TELEGRAM_WEBHOOK_SECRET
```

If a secret container already exists, skip its `gcloud secrets create` command and add a new
version. Store only the bot username, without `@`, as a non-secret GitHub variable:

```bash
gh variable set EVA_TELEGRAM_BOT_USERNAME --body YOUR_BOT_USERNAME
gh variable set EVA_TELEGRAM_ENABLED --body true
```

Set `EVA_TELEGRAM_ENABLED` only after both secret versions exist. Until then it defaults to false,
so ordinary backend deployments do not require placeholder Telegram credentials. Terraform grants
the API access to the webhook secret and injects the bot token into the worker. Secret values are
never Terraform variables.

## 2. Deploy and register the webhook

Merge the Milestone 7 PR after both secret versions exist. The main-branch deployment creates the
Telegram Pub/Sub resources, applies the database migration, and deploys the API and worker. Keep
`EVA_WORKER_INSTANCE_COUNT=0` until the production User, Workspace, Gmail connector, and Telegram
account are ready. A zero worker count safely queues durable Pub/Sub messages but does not answer
them.

Load the secrets into a local shell without printing them, set the deployed API URL, and register
the webhook:

```bash
export EVA_TELEGRAM_BOT_TOKEN="$(gcloud secrets versions access latest \
  --secret=eva-telegram-bot-token)"
export EVA_TELEGRAM_WEBHOOK_SECRET="$(gcloud secrets versions access latest \
  --secret=eva-telegram-webhook-secret)"
export EVA_TELEGRAM_BOT_USERNAME=YOUR_BOT_USERNAME
export EVA_TELEGRAM_ENABLED=true
export EVA_TELEGRAM_TURN_TOPIC_ID=eva-telegram-turns
export EVA_TELEGRAM_TURN_SUBSCRIPTION_ID=eva-telegram-turns-production
export EVA_TELEGRAM_DELIVERY_TOPIC_ID=eva-telegram-delivery
export EVA_TELEGRAM_DELIVERY_SUBSCRIPTION_ID=eva-telegram-delivery-production
export EVA_PUBSUB_PROJECT_ID=GCP_PROJECT_ID
export EVA_RELEVANCE_ENABLED=true
export EVA_AGENT_ENABLED=true
export EVA_OPENAI_API_KEY="$(gcloud secrets versions access latest \
  --secret=eva-openai-api-key)"

uv run eva telegram webhook set --url "https://API_HOST/webhooks/telegram"
uv run eva telegram webhook status
```

Unset the three secret environment variables when the operator commands finish. Webhook status
must show the expected HTTPS URL and no pending provider error.

## 3. Pair a Telegram account

Run the command against the production database, normally through the Cloud SQL Auth Proxy. Use
the existing scoped UUIDs; never infer scope from a Telegram username:

```bash
uv run eva telegram pairing create \
  --user-id USER_UUID \
  --workspace-id WORKSPACE_UUID
```

The command returns a short-lived `https://t.me/...` start link. Open it from the intended private
Telegram account and press Start. Eva stores only a digest of the pairing token, consumes it once,
and binds the immutable numeric Telegram user ID and private chat ID to that Eva scope. Display
names and usernames are metadata, not identity.

Inspect or revoke mappings explicitly:

```bash
uv run eva telegram account list \
  --user-id USER_UUID --workspace-id WORKSPACE_UUID

uv run eva telegram account revoke \
  --user-id USER_UUID --workspace-id WORKSPACE_UUID \
  --account-id TELEGRAM_ACCOUNT_UUID
```

After the Gmail connector and Telegram account are present in Cloud SQL, set the worker count to
one and manually deploy:

```bash
gh variable set EVA_WORKER_INSTANCE_COUNT --body 1
gh workflow run "Deploy Eva to GCP"
```

## 4. Smoke test

Send an ordinary private message to the bot. Eva should answer within a few seconds. Send a second
message to verify context continuity, then `/new` to create a fresh general conversation. Replying
directly to a proactive Eva message should recover its originating Gmail Situation and memory.

Inspect delivery state without exposing message text by default:

```bash
uv run eva notification list \
  --user-id USER_UUID --workspace-id WORKSPACE_UUID

uv run eva notification show \
  --user-id USER_UUID --workspace-id WORKSPACE_UUID \
  --notification-id NOTIFICATION_UUID
```

Use `--include-content` only in a trusted terminal. A failed notification can be requeued explicitly:

```bash
uv run eva notification retry \
  --user-id USER_UUID --workspace-id WORKSPACE_UUID \
  --notification-id NOTIFICATION_UUID
```

## Safety and failure behavior

- Only private chats are accepted. Unknown or revoked accounts are acknowledged without becoming
  an identity oracle.
- Telegram IDs, User ID, and Workspace ID must all match on every lookup and mutation.
- Incoming text is authenticated user intent after pairing, but it cannot bypass policy or grant
  new tools. Quoted email and Gmail tool results remain untrusted evidence.
- General chat can use bounded Gmail search for the scoped active connector. Reading a fixed Gmail
  thread is available only when the conversation is linked to that email Situation.
- Gmail remains read-only. Draft/send and Telegram approval callbacks arrive in Milestone 8.
- Pub/Sub delivery is at-least-once. Database idempotency prevents duplicate model runs and normal
  duplicate sends. A Telegram timeout after provider acceptance remains inherently ambiguous and
  may require operator inspection.
- Provider, quota, and transient transport failures retry with bounded backoff. Invalid scope,
  revoked accounts, malformed output, and exhausted attempts terminate safely.
- Logs and default CLI output omit Telegram text, conversation history, email content, search
  queries, credentials, prompts, raw provider responses, and raw exceptions.

## Troubleshooting

- **Webhook returns 401:** confirm the Secret Manager value used by the API is the same value passed
  to `setWebhook`; adding a new secret version requires a new Cloud Run revision to read `latest`.
- **Messages queue but Eva is silent:** confirm the worker count is one, both Telegram subscriptions
  exist, the relay is running, and `EVA_TELEGRAM_ENABLED=true` is present on the worker.
- **Pairing link expired:** create a new pairing code. Old codes cannot be extended or reused.
- **Bot replies without Gmail evidence:** confirm an active Gmail connector exists for the exact
  paired User and Workspace. Eva can still answer without Gmail tools if authorization is absent or
  revoked.
- **Proactive reply loses context:** verify the original Notification reached `SENT` and has its
  Telegram provider message ID. Replies to unknown message IDs fall back to the active general
  conversation.
- **Rotating the bot token:** add a Secret Manager version, redeploy the worker, then register/check
  the webhook using the new token. Revoke the old token through BotFather if it was exposed.
- **Rotating the webhook secret:** add a new version, redeploy the API, and immediately call
  `setWebhook` with the new value.
