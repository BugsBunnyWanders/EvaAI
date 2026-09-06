output "api_url" {
  description = "Public Eva API URL."
  value       = google_cloud_run_v2_service.api.uri
}

output "cloud_sql_connection_name" {
  description = "Cloud SQL connection name used by Cloud Run."
  value       = google_sql_database_instance.eva.connection_name
}

output "gmail_subscription" {
  description = "Production Gmail notification subscription."
  value       = google_pubsub_subscription.gmail.name
}

output "relevance_subscription" {
  description = "Production Eva event subscription."
  value       = google_pubsub_subscription.relevance.name
}
