resource "google_cloud_run_v2_service" "api" {
  project             = var.project_id
  name                = "eva-api"
  location            = var.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.api.email
    max_instance_request_concurrency = 40

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.eva.connection_name]
      }
    }

    containers {
      name  = "api"
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      startup_probe {
        http_get {
          path = "/health/live"
          port = 8080
        }
        initial_delay_seconds = 1
        period_seconds        = 2
        timeout_seconds       = 1
        failure_threshold     = 10
      }

      dynamic "env" {
        for_each = local.common_environment
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "EVA_DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }
  }

  depends_on = [
    google_project_iam_member.api_cloud_sql,
    google_secret_manager_secret_iam_member.api_database,
    google_secret_manager_secret_version.database_url,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_api" {
  project  = var.project_id
  location = google_cloud_run_v2_service.api.location
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_worker_pool" "worker" {
  project             = var.project_id
  name                = "eva-worker"
  location            = var.region
  deletion_protection = true
  description         = "Continuous Gmail ingestion, transactional outbox, and relevance loops"

  template {
    service_account = google_service_account.worker.email

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.eva.connection_name]
      }
    }

    containers {
      name    = "worker"
      image   = var.image
      command = ["eva"]
      args    = ["worker", "run"]

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      dynamic "env" {
        for_each = local.worker_environment
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "EVA_DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "EVA_OPENAI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.openai_api_key.secret_id
            version = "latest"
          }
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }
  }

  scaling {
    scaling_mode          = "MANUAL"
    manual_instance_count = var.worker_instance_count
  }

  instance_splits {
    type    = "INSTANCE_SPLIT_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  depends_on = [
    google_project_iam_member.worker_roles,
    google_pubsub_subscription.gmail,
    google_pubsub_subscription.relevance,
    google_secret_manager_secret_version.database_url,
  ]
}

resource "google_cloud_run_v2_job" "migrate" {
  project             = var.project_id
  name                = "eva-migrate"
  location            = var.region
  deletion_protection = true

  template {
    task_count = 1

    template {
      service_account = google_service_account.api.email
      max_retries     = 1
      timeout         = "900s"

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.eva.connection_name]
        }
      }

      containers {
        name    = "migrate"
        image   = var.image
        command = ["alembic"]
        args    = ["upgrade", "head"]

        env {
          name = "EVA_DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.database_url.secret_id
              version = "latest"
            }
          }
        }

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }
    }
  }

  depends_on = [
    google_project_iam_member.api_cloud_sql,
    google_secret_manager_secret_iam_member.api_database,
    google_secret_manager_secret_version.database_url,
  ]
}

resource "google_cloud_run_v2_job" "gmail_maintenance" {
  project             = var.project_id
  name                = "eva-gmail-maintenance"
  location            = var.region
  deletion_protection = true

  template {
    task_count = 1

    template {
      service_account = google_service_account.worker.email
      max_retries     = 2
      timeout         = "900s"

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.eva.connection_name]
        }
      }

      containers {
        name    = "gmail-maintenance"
        image   = var.image
        command = ["eva"]
        args    = ["gmail", "maintain"]

        dynamic "env" {
          for_each = local.maintenance_environment
          content {
            name  = env.key
            value = env.value
          }
        }

        env {
          name = "EVA_DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.database_url.secret_id
              version = "latest"
            }
          }
        }

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }
    }
  }

  depends_on = [
    google_project_iam_member.worker_roles,
    google_secret_manager_secret_version.database_url,
  ]
}
