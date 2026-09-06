terraform {
  required_version = ">= 1.16.0, < 2.0.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.43"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # The bucket is supplied by `terraform init -backend-config=...` because backend blocks
  # cannot use input variables. The bootstrap stack creates the versioned bucket first.
  backend "gcs" {
    prefix = "eva/production"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
