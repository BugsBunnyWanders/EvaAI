resource "random_password" "database" {
  length  = 40
  special = false
}

resource "google_sql_database_instance" "eva" {
  project          = var.project_id
  name             = "eva-postgres"
  region           = var.region
  database_version = "POSTGRES_17"

  # Both provider-level and API-level safeguards must be deliberately disabled before deletion.
  deletion_protection = true

  settings {
    tier              = var.database_tier
    edition           = "ENTERPRISE"
    availability_type = "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = 10
    disk_autoresize   = true

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "18:30"
      transaction_log_retention_days = 7
    }

    ip_configuration {
      ipv4_enabled = true
    }

    user_labels = {
      application = "eva"
      environment = "production"
    }
  }
}

resource "google_sql_database" "eva" {
  project  = var.project_id
  name     = local.database_name
  instance = google_sql_database_instance.eva.name
}

resource "google_sql_user" "eva" {
  project  = var.project_id
  name     = local.database_user
  instance = google_sql_database_instance.eva.name
  password = random_password.database.result
}

resource "google_secret_manager_secret" "database_url" {
  project   = var.project_id
  secret_id = local.database_secret_id

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "database_url" {
  secret = google_secret_manager_secret.database_url.id
  # The alphanumeric generated password needs no URL escaping. Cloud Run reaches Cloud SQL through
  # its authenticated Unix socket, so no database address is exposed to the public internet.
  secret_data = "postgresql+psycopg://${local.database_user}:${random_password.database.result}@/${local.database_name}?host=/cloudsql/${google_sql_database_instance.eva.connection_name}"

  depends_on = [google_sql_database.eva, google_sql_user.eva]
}

resource "google_secret_manager_secret_iam_member" "api_database" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}
