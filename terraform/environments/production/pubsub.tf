resource "google_pubsub_topic" "gmail" {
  project = var.project_id
  name    = var.gmail_topic_id
}

resource "google_pubsub_topic" "events" {
  project = var.project_id
  name    = var.events_topic_id
}

resource "google_pubsub_topic" "agent_runs" {
  project = var.project_id
  name    = var.agent_topic_id
}

resource "google_pubsub_topic" "telegram_turns" {
  project = var.project_id
  name    = var.telegram_turn_topic_id
}

resource "google_pubsub_topic" "telegram_delivery" {
  project = var.project_id
  name    = var.telegram_delivery_topic_id
}

# These topics predate Terraform. Declarative imports adopt them on the first production apply.
import {
  to = google_pubsub_topic.gmail
  id = "projects/${var.project_id}/topics/${var.gmail_topic_id}"
}

import {
  to = google_pubsub_topic.events
  id = "projects/${var.project_id}/topics/${var.events_topic_id}"
}

resource "google_pubsub_topic_iam_member" "gmail_push" {
  project = var.project_id
  topic   = google_pubsub_topic.gmail.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:gmail-api-push@system.gserviceaccount.com"
}

resource "google_pubsub_subscription" "gmail" {
  project              = var.project_id
  name                 = local.gmail_subscription
  topic                = google_pubsub_topic.gmail.id
  ack_deadline_seconds = 600

  expiration_policy {
    ttl = ""
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
}

resource "google_pubsub_subscription" "relevance" {
  project              = var.project_id
  name                 = local.events_subscription
  topic                = google_pubsub_topic.events.id
  ack_deadline_seconds = 600

  expiration_policy {
    ttl = ""
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
}

resource "google_pubsub_subscription" "agent" {
  project              = var.project_id
  name                 = local.agent_subscription
  topic                = google_pubsub_topic.agent_runs.id
  ack_deadline_seconds = 600

  expiration_policy {
    ttl = ""
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
}

resource "google_pubsub_subscription" "telegram_turns" {
  project              = var.project_id
  name                 = local.telegram_turn_subscription
  topic                = google_pubsub_topic.telegram_turns.id
  ack_deadline_seconds = 600

  expiration_policy {
    ttl = ""
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
}

resource "google_pubsub_subscription" "telegram_delivery" {
  project              = var.project_id
  name                 = local.telegram_delivery_subscription
  topic                = google_pubsub_topic.telegram_delivery.id
  ack_deadline_seconds = 600

  expiration_policy {
    ttl = ""
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
}
