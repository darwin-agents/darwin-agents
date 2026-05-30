# Default values for the local stack. Override per-developer via
# terraform.tfvars.local (gitignored) — Terraform auto-loads any file
# whose name ends in .auto.tfvars or matches terraform.tfvars{,.json}.

cluster_name       = "darwin"
platform_namespace = "darwin"
host_api_port      = 8000

# Use the image you've built locally via:
#   docker build -t darwin-platform:dev .
# (or point at a published image once you have one).
platform_image = "darwin-platform:dev"
