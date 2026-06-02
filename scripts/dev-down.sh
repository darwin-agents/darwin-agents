#!/usr/bin/env bash
#
# dev-down.sh — tear down everything `dev-up.sh` started: the web dashboard
# container and the full kind stack (Helm releases + the kind cluster).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TF_DIR="infra/terraform/envs/local"
KUBECONFIG_PATH="$REPO_ROOT/$TF_DIR/.kubeconfig"
COMPOSE=(docker compose -f infra/docker/docker-compose.yml --env-file .env)

say() { printf '\033[36m▸ %s\033[0m\n' "$*"; }

# 1. stop the dashboard container (ignore if it was never started).
say "stopping dashboard container"
"${COMPOSE[@]}" rm -sf web >/dev/null 2>&1 || true

# 2. destroy the kind stack + cluster.
say "destroying kind stack + cluster"
terraform -chdir="$TF_DIR" destroy -auto-approve -input=false

# 3. drop the stale kubeconfig.
rm -f "$KUBECONFIG_PATH"

printf '\033[32m✓ local stack torn down.\033[0m\n'
