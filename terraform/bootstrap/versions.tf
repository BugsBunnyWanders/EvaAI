terraform {
  required_version = ">= 1.16.0, < 2.0.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.43"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
