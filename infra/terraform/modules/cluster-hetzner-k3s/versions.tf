terraform {
  required_version = ">= 1.6.0"

  required_providers {
    hcloud   = { source = "hetznercloud/hcloud", version = "~> 1.64" } # 1.64.0 (2026-05)
    random   = { source = "hashicorp/random", version = "~> 3.6" }
    null     = { source = "hashicorp/null", version = "~> 3.2" }
    local    = { source = "hashicorp/local", version = "~> 2.5" }
    external = { source = "hashicorp/external", version = "~> 2.3" }
  }
}
