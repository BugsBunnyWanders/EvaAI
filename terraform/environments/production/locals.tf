locals {
  database_name                  = "eva"
  database_user                  = "eva_app"
  database_secret_id             = "eva-database-url"
  gmail_subscription             = "eva-gmail-ingestion-production"
  events_subscription            = "eva-relevance-production"
  agent_subscription             = "eva-agent-production"
  telegram_turn_subscription     = "eva-telegram-turns-production"
  telegram_delivery_subscription = "eva-telegram-delivery-production"
  action_dispatch_subscription   = "eva-action-dispatch-production"
  action_executor_default_url    = "https://eva-action-executor-${data.google_project.current.number}.${var.region}.run.app"

  action_environment = {
    EVA_ACTIONS_ENABLED                    = tostring(var.actions_enabled)
    EVA_ACTION_DISPATCH_SUBSCRIPTION_ID    = local.action_dispatch_subscription
    EVA_ACTION_TASKS_PROJECT_ID            = var.project_id
    EVA_ACTION_TASKS_LOCATION              = var.region
    EVA_ACTION_TASKS_QUEUE_ID              = var.action_queue_id
    EVA_ACTION_EXECUTOR_URL                = "${local.action_executor_default_url}/internal/actions/execute"
    EVA_ACTION_EXECUTOR_AUDIENCE           = local.action_executor_default_url
    EVA_ACTION_TASK_CALLER_SERVICE_ACCOUNT = google_service_account.action_task_caller.email
    EVA_ACTION_APPROVAL_TTL_HOURS          = "24"
    EVA_ACTION_REVISION_TTL_SECONDS        = "900"
    EVA_ACTION_LEASE_SECONDS               = "300"
    EVA_ACTION_TASK_TIMEOUT_SECONDS        = tostring(var.action_task_timeout_seconds)
    EVA_ACTION_TASK_MAX_ATTEMPTS           = tostring(var.action_task_max_attempts)
  }

  common_environment = {
    EVA_APP_NAME                   = "Eva"
    EVA_ENVIRONMENT                = "production"
    EVA_LOG_FORMAT                 = "json"
    EVA_LOG_LEVEL                  = "INFO"
    EVA_PUBSUB_PROJECT_ID          = var.project_id
    EVA_PUBSUB_TOPIC_ID            = var.events_topic_id
    EVA_AGENT_TOPIC_ID             = var.agent_topic_id
    EVA_GMAIL_TOPIC_ID             = var.gmail_topic_id
    EVA_TELEGRAM_TURN_TOPIC_ID     = var.telegram_turn_topic_id
    EVA_TELEGRAM_DELIVERY_TOPIC_ID = var.telegram_delivery_topic_id
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

  worker_environment = merge(local.common_environment, local.gmail_sync_environment, local.action_environment, {
    EVA_GMAIL_SUBSCRIPTION_ID             = local.gmail_subscription
    EVA_RELEVANCE_ENABLED                 = "true"
    EVA_RELEVANCE_SUBSCRIPTION_ID         = local.events_subscription
    EVA_AGENT_ENABLED                     = "true"
    EVA_AGENT_SUBSCRIPTION_ID             = local.agent_subscription
    EVA_TELEGRAM_ENABLED                  = tostring(var.telegram_enabled)
    EVA_TELEGRAM_TURN_SUBSCRIPTION_ID     = local.telegram_turn_subscription
    EVA_TELEGRAM_DELIVERY_SUBSCRIPTION_ID = local.telegram_delivery_subscription
  })

  api_environment = merge(local.common_environment, local.action_environment, var.telegram_bot_username == "" ? {} : {
    EVA_TELEGRAM_BOT_USERNAME = var.telegram_bot_username
  })

  maintenance_environment = merge(local.common_environment, local.gmail_sync_environment, {
    EVA_GMAIL_SUBSCRIPTION_ID = local.gmail_subscription
    EVA_RELEVANCE_ENABLED     = "false"
    EVA_AGENT_ENABLED         = "false"
  })

  action_executor_environment = merge(local.common_environment, local.action_environment, {
    EVA_RELEVANCE_ENABLED = "false"
    EVA_AGENT_ENABLED     = "false"
    EVA_TELEGRAM_ENABLED  = "false"
  })
}
