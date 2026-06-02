terraform {
  required_version = ">= 1.6.0"

  required_providers {
    hcloud     = { source = "hetznercloud/hcloud", version = "~> 1.64" }
    helm       = { source = "hashicorp/helm", version = "~> 2.13" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.30" }
    kubectl    = { source = "alekc/kubectl", version = "~> 2.0" }
    random     = { source = "hashicorp/random", version = "~> 3.6" }
    null       = { source = "hashicorp/null", version = "~> 3.2" }
    local      = { source = "hashicorp/local", version = "~> 2.5" }
    external   = { source = "hashicorp/external", version = "~> 2.3" }
  }
}
