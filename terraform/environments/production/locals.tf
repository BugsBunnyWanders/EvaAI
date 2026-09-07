locals {
  database_name       = "eva"
  database_user       = "eva_app"
  database_secret_id  = "eva-database-url"
  gmail_subscription  = "eva-gmail-ingestion-production"
  events_subscription = "eva-relevance-production"

  common_environment = {
    EVA_APP_NAME          = "Eva"
    EVA_ENVIRONMENT       = "production"
    EVA_LOG_FORMAT        = "json"
    EVA_LOG_LEVEL         = "INFO"
    EVA_PUBSUB_PROJECT_ID = var.project_id
    EVA_PUBSUB_TOPIC_ID   = var.events_topic_id
    EVA_GMAIL_TOPIC_ID    = var.gmail_topic_id
  }

  # Backlog synchronization can cross Gmail quota windows. Keep the database lease
  # valid while the client performs bounded exponential backoff instead of restarting
  # the entire history range and immediately consuming the same quota again.
  gmail_sync_environment = {
    EVA_GMAIL_SYNC_LEASE_SECONDS            = "900"
    EVA_GMAIL_RETRY_ATTEMPTS                = "8"
    EVA_GMAIL_RETRY_INITIAL_BACKOFF_SECONDS = "1.0"
    EVA_GMAIL_RETRY_MAX_BACKOFF_SECONDS     = "60.0"
  }

  worker_environment = merge(local.common_environment, local.gmail_sync_environment, {
    EVA_GMAIL_SUBSCRIPTION_ID     = local.gmail_subscription
    EVA_RELEVANCE_ENABLED         = "true"
    EVA_RELEVANCE_SUBSCRIPTION_ID = local.events_subscription
  })

  maintenance_environment = merge(local.common_environment, local.gmail_sync_environment, {
    EVA_GMAIL_SUBSCRIPTION_ID = local.gmail_subscription
    EVA_RELEVANCE_ENABLED     = "false"
  })
}
