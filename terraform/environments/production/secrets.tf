data "google_secret_manager_secret" "openai_api_key" {
  project   = var.project_id
  secret_id = var.openai_secret_id
}

data "google_secret_manager_secret" "telegram_bot_token" {
  count     = var.telegram_enabled ? 1 : 0
  project   = var.project_id
  secret_id = var.telegram_bot_token_secret_id
}

data "google_secret_manager_secret" "telegram_webhook_secret" {
  count     = var.telegram_enabled ? 1 : 0
  project   = var.project_id
  secret_id = var.telegram_webhook_secret_id
}

resource "google_secret_manager_secret_iam_member" "api_telegram_webhook" {
  count     = var.telegram_enabled ? 1 : 0
  secret_id = data.google_secret_manager_secret.telegram_webhook_secret[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}
