variable "project_id" {
  description = "Google Cloud project that hosts Eva."
  type        = string
}

variable "region" {
  description = "Google Cloud region for Eva's runtime and database."
  type        = string
  default     = "asia-south1"
}

variable "image" {
  description = "Immutable Eva container image URI, including its sha256 digest."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image))
    error_message = "image must be pinned to an immutable sha256 digest"
  }
}

variable "database_tier" {
  description = "Cloud SQL machine tier. Increase this before multi-user workloads."
  type        = string
  default     = "db-f1-micro"
}

variable "worker_instance_count" {
  description = "Continuous worker instances. Deployments temporarily set this to zero for migrations."
  type        = number
  default     = 1

  validation {
    condition     = var.worker_instance_count >= 0 && var.worker_instance_count <= 1
    error_message = "the personal MVP supports zero or one worker instance"
  }
}

variable "gmail_topic_id" {
  description = "Existing Gmail push-notification topic."
  type        = string
  default     = "eva-gmail-notifications"
}

variable "events_topic_id" {
  description = "Existing Eva domain-event topic."
  type        = string
  default     = "eva-events"
}

variable "agent_topic_id" {
  description = "Eva agent investigation request topic."
  type        = string
  default     = "eva-agent-runs"
}

variable "telegram_turn_topic_id" {
  description = "Eva authenticated Telegram conversation-turn topic."
  type        = string
  default     = "eva-telegram-turns"
}

variable "telegram_delivery_topic_id" {
  description = "Eva Telegram Notification delivery topic."
  type        = string
  default     = "eva-telegram-delivery"
}

variable "telegram_bot_username" {
  description = "Telegram bot username without the leading at-sign."
  type        = string
  default     = ""
}

variable "telegram_enabled" {
  description = "Enable the Telegram API secret and worker conversation/delivery loops."
  type        = bool
  default     = false
}

variable "openai_secret_id" {
  description = "Secret created by the bootstrap stack; its value is added out of band."
  type        = string
  default     = "eva-openai-api-key"
}

variable "telegram_bot_token_secret_id" {
  description = "Existing Secret Manager secret containing the Telegram bot token."
  type        = string
  default     = "eva-telegram-bot-token"
}

variable "telegram_webhook_secret_id" {
  description = "Existing Secret Manager secret containing the Telegram webhook secret token."
  type        = string
  default     = "eva-telegram-webhook-secret"
}
