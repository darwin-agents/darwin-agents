#!/usr/bin/env bash
#
# dev-up.sh — single-command local bring-up of the full Darwin platform on
# kind, plus the web dashboard. Wraps the Terraform local env and folds in
# the three steps a bare `terraform apply` can't do on its own:
#
#   1. build the supervisor image and `kind load` it BEFORE the platform
#      Helm release (wait=true) — otherwise it hangs on ImagePullBackOff
#      because kind nodes don't share the host's Docker images.
#   2. refresh the Helm repo cache (the Terraform helm provider chokes on a
#      stale/empty ~/.cache/helm cache: "no cached repo found: *-index.yaml").
#   3. start ONLY the dashboard container (`--no-deps`) so Compose doesn't
#      also boot its own supervisor on :8000 and collide with the kind ingress.
#
# Idempotent: safe to re-run. A re-run rebuilds the image and rolls the
# supervisor so local code changes take effect.
#
# Env knobs:
#   SKIP_BUILD=1       skip the supervisor image rebuild
#   WITH_DASHBOARD=0   skip the web dashboard
#   CLUSTER=darwin     kind cluster name
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TF_DIR="infra/terraform/envs/local"
TF=(terraform -chdir="$TF_DIR")
KUBECONFIG_PATH="$REPO_ROOT/$TF_DIR/.kubeconfig"
IMAGE="darwin-platform:dev"
CLUSTER="${CLUSTER:-darwin}"
NS="darwin"
API_URL="http://localhost:8000"
DASH_URL="http://localhost:3000"
SKIP_BUILD="${SKIP_BUILD:-0}"
WITH_DASHBOARD="${WITH_DASHBOARD:-1}"
COMPOSE=(docker compose -f infra/docker/docker-compose.yml --env-file .env)

say() { printf '\033[36m▸ %s\033[0m\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# 1. preflight — fail loud on a missing tool rather than mid-apply.
for bin in docker kind kubectl terraform helm; do
  command -v "$bin" >/dev/null 2>&1 || die "missing required tool: $bin"
done
[ -f .env ] || { say "creating .env from .env.example"; cp .env.example .env; }

# 2. build the supervisor image (covers both supervisor + orchestrator pods).
if [ "$SKIP_BUILD" != "1" ]; then
  say "building $IMAGE"
  docker build -f infra/docker/Dockerfile -t "$IMAGE" .
else
  say "skipping image build (SKIP_BUILD=1)"
fi

# 3. terraform init (idempotent).
say "terraform init"
"${TF[@]}" init -input=false >/dev/null

# 4. create the kind cluster FIRST so we can load the image before the
#    platform release waits on it.
say "creating kind cluster '$CLUSTER' (if absent)"
"${TF[@]}" apply -target=module.cluster -auto-approve -input=false

# 5. load the local image into every kind node.
say "loading $IMAGE into kind"
kind load docker-image "$IMAGE" --name "$CLUSTER"

# 6. refresh the Helm repo cache (defensive; no-op on a clean machine).
helm repo list >/dev/null 2>&1 && { say "helm repo update"; helm repo update >/dev/null 2>&1 || true; }

# 7. note whether the supervisor already exists (so a re-run picks up new code).
export KUBECONFIG="$KUBECONFIG_PATH"
SUP_EXISTED=0
kubectl -n "$NS" get deploy darwin-platform >/dev/null 2>&1 && SUP_EXISTED=1

# 8. apply the full stack.
say "applying full platform stack"
"${TF[@]}" apply -auto-approve -input=false

# 9. if this was a refresh (deployment pre-existed) and we rebuilt, roll the
#    supervisor so the freshly-loaded image's code takes effect.
if [ "$SUP_EXISTED" = "1" ] && [ "$SKIP_BUILD" != "1" ]; then
  say "rolling supervisor to pick up rebuilt image"
  kubectl -n "$NS" rollout restart deploy/darwin-platform
  kubectl -n "$NS" rollout status deploy/darwin-platform --timeout=120s
fi

# 10. wait for the API.
printf '\033[36m▸ waiting for %s/readyz \033[0m' "$API_URL"
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$API_URL/readyz" || true)"
  [ "$code" = "200" ] && { printf 'ok\n'; break; }
  printf '.'; sleep 2
done

# 11. dashboard — start ONLY the web container (no compose supervisor/PG).
if [ "$WITH_DASHBOARD" = "1" ]; then
  say "starting dashboard (compose web, --no-deps)"
  "${COMPOSE[@]}" up -d --no-deps --build web \
    || printf '\033[33m  (dashboard skipped — check web/ build or infra/docker)\033[0m\n'
fi

cat <<EOF

$(printf '\033[32m✓ Darwin local stack is up.\033[0m')
  API:        $API_URL        (health: $API_URL/readyz)
  Dashboard:  $DASH_URL
  kubeconfig: export KUBECONFIG=$KUBECONFIG_PATH

  kubectl get pods -A
  make dev-down     # tear it all down
EOF
