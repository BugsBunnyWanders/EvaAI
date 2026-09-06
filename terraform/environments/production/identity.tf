resource "google_service_account" "api" {
  project      = var.project_id
  account_id   = "eva-api"
  display_name = "Eva API runtime"
}

resource "google_service_account" "worker" {
  project      = var.project_id
  account_id   = "eva-worker"
  display_name = "Eva continuous worker runtime"
}

resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = "eva-scheduler"
  display_name = "Eva Cloud Scheduler invoker"
}

resource "google_project_iam_member" "api_cloud_sql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "worker_roles" {
  for_each = toset([
    "roles/cloudsql.client",
    "roles/pubsub.publisher",
    "roles/pubsub.subscriber",
    "roles/secretmanager.secretAccessor",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "scheduler_run_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.scheduler.email}"
}
