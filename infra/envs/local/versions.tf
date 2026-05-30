terraform {
  required_version = ">= 1.6.0"

  required_providers {
    kind = {
      # tehcyx is the maintained fork; the original repo went stale in 2024.
      source  = "tehcyx/kind"
      version = "~> 0.7"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.13"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
    kubectl = {
      # alekc fork — actively maintained, doesn't validate CRDs at plan
      # time, which is exactly what a single-apply infra/ needs.
      source  = "alekc/kubectl"
      version = "~> 2.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
