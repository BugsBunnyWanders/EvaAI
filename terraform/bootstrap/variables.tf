variable "project_id" {
  description = "Google Cloud project that hosts Eva."
  type        = string
}

variable "region" {
  description = "Primary Google Cloud region."
  type        = string
  default     = "asia-south1"
}

variable "github_repository" {
  description = "GitHub owner/repository allowed to deploy Eva."
  type        = string
  default     = "BugsBunnyWanders/EvaAI"
}

variable "state_bucket_name" {
  description = "Globally unique GCS bucket name for production Terraform state."
  type        = string
  default     = "evaai-507018-terraform-state"
}
