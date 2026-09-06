data "google_secret_manager_secret" "openai_api_key" {
  project   = var.project_id
  secret_id = var.openai_secret_id
}
