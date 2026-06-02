terraform {
  required_version = ">= 1.6.0"

  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.13"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
    # Declared so the alekc/kubectl provider the root configures resolves
    # correctly when passed through to the cnpg child (which applies the
    # Postgres Cluster CR via kubectl_manifest).
    kubectl = {
      source  = "alekc/kubectl"
      version = "~> 2.0"
    }
  }
}
