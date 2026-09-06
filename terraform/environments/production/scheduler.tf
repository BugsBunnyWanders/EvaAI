resource "google_cloud_scheduler_job" "gmail_maintenance" {
  project          = var.project_id
  region           = var.region
  name             = "eva-gmail-maintenance"
  description      = "Periodic safety sync and Gmail watch renewal"
  schedule         = "17 * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "900s"

  retry_config {
    retry_count          = 2
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
    max_retry_duration   = "900s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.gmail_maintenance.name}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [google_project_iam_member.scheduler_run_invoker]
}
