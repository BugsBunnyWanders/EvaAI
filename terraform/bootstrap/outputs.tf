output "artifact_registry_repository" {
  description = "Artifact Registry repository used by the release workflow."
  value       = google_artifact_registry_repository.eva.name
}

output "deployer_service_account" {
  description = "Set this value as the GCP_DEPLOY_SERVICE_ACCOUNT GitHub variable."
  value       = google_service_account.github_deployer.email
}

output "state_bucket" {
  description = "Set this value as the TF_STATE_BUCKET GitHub variable."
  value       = google_storage_bucket.terraform_state.name
}

output "workload_identity_provider" {
  description = "Set this value as the GCP_WORKLOAD_IDENTITY_PROVIDER GitHub variable."
  value       = google_iam_workload_identity_pool_provider.github.name
}
