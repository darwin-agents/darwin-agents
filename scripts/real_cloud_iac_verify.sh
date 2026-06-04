#!/usr/bin/env bash
# Real-cloud IaC verification (docs/engineering/20 §6, §4b; 17 §5b).
#
# The audit's standing gap: PITR, Vault auto-unseal, and Kata microVMs are
# only STRUCTURALLY validated (`terraform validate` green) — never run against
# real infra, because each needs hardware/services this repo's CI can't
# provide (an object store, a transit/KMS seal, nested-virt nodes). This
# script is the operator's one-command bridge from "validates" to "works":
#
#   Phase A (structural, no cloud needed): terraform fmt + validate in each env.
#   Phase B (runtime, needs a live cluster): for each gated feature that the
#     env has ENABLED, prove it actually functions — fail loudly if an enabled
#     feature is broken; SKIP (not fail) a feature left off by design.
#
# Read-only against the cluster (one optional throwaway Kata test pod, removed
# after). Degrades gracefully: a missing tool (terraform/kubectl/vault/jq)
# downgrades the dependent checks to SKIP with a clear reason, so the script
# is safe to run from a laptop or a CI box.
#
# Usage:
#   scripts/real_cloud_iac_verify.sh                  # both phases, hetzner env
#   scripts/real_cloud_iac_verify.sh --skip-runtime   # structural only (no cloud)
#   scripts/real_cloud_iac_verify.sh --env local
#   scripts/real_cloud_iac_verify.sh --pg-namespace darwin --vault-namespace vault
#
# Exit 0 = every applicable check passed (skips don't fail); non-zero = an
# enabled feature failed its runtime check, or structural validation failed.
set -uo pipefail

ENV_NAME="hetzner"
TF_ROOT="infra/terraform/envs"
PG_NS="darwin"
VAULT_NS="vault"
SKIP_TF=0
SKIP_RUNTIME=0
KEEP_KATA_POD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_NAME="$2"; shift 2;;
    --pg-namespace) PG_NS="$2"; shift 2;;
    --vault-namespace) VAULT_NS="$2"; shift 2;;
    --skip-terraform) SKIP_TF=1; shift;;
    --skip-runtime) SKIP_RUNTIME=1; shift;;
    --keep-kata-pod) KEEP_KATA_POD=1; shift;;
    -h|--help) sed -n '2,30p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

C_R='\033[0;31m'; C_G='\033[0;32m'; C_Y='\033[0;33m'; C_B='\033[0;34m'; C_X='\033[0m'
PASS=0; FAIL=0; SKIP=0
pass() { echo -e "  ${C_G}PASS${C_X} $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${C_R}FAIL${C_X} $1"; FAIL=$((FAIL+1)); }
skip() { echo -e "  ${C_Y}SKIP${C_X} $1"; SKIP=$((SKIP+1)); }
hdr()  { echo -e "\n${C_B}== $1 ==${C_X}"; }

have() { command -v "$1" >/dev/null 2>&1; }

ENV_DIR="$TF_ROOT/$ENV_NAME"

# --------------------------------------------------------------------------
# Phase A — structural validation (no cloud required)
# --------------------------------------------------------------------------
phase_a() {
  hdr "Phase A — structural (terraform fmt + validate)"
  if ! have terraform; then
    skip "terraform not installed → cannot validate (CI ran this green; install to re-check)"
    return
  fi
  if [[ ! -d "$ENV_DIR" ]]; then
    fail "env dir $ENV_DIR not found (run from repo root, or pass --env)"
    return
  fi
  if terraform -chdir="$ENV_DIR" fmt -check -recursive >/dev/null 2>&1; then
    pass "terraform fmt -check clean ($ENV_DIR)"
  else
    fail "terraform fmt -check found unformatted files ($ENV_DIR)"
  fi
  # validate needs `init` (providers); use -backend=false so it works offline.
  if terraform -chdir="$ENV_DIR" init -backend=false -input=false >/tmp/tfinit.log 2>&1; then
    if terraform -chdir="$ENV_DIR" validate >/tmp/tfvalidate.log 2>&1; then
      pass "terraform validate green ($ENV_DIR)"
    else
      fail "terraform validate failed ($ENV_DIR) — see /tmp/tfvalidate.log"
    fi
  else
    skip "terraform init -backend=false failed (offline providers?) — see /tmp/tfinit.log"
  fi
}

# --------------------------------------------------------------------------
# Phase B — runtime checks against a live cluster
# --------------------------------------------------------------------------
kubectl_ok() { kubectl version --request-timeout=5s >/dev/null 2>&1; }

check_pitr() {
  hdr "Phase B.1 — Postgres PITR (CNPG barmanObjectStore + scheduled backup)"
  local cl
  cl=$(kubectl -n "$PG_NS" get cluster.postgresql.cnpg.io -o name 2>/dev/null | head -1)
  if [[ -z "$cl" ]]; then
    skip "no CNPG Cluster in ns/$PG_NS → PITR not deployed here (disabled by design?)"
    return
  fi
  # barmanObjectStore configured?
  if [[ -n "$(kubectl -n "$PG_NS" get "$cl" -o jsonpath='{.spec.backup.barmanObjectStore.destinationPath}' 2>/dev/null)" ]]; then
    pass "barmanObjectStore.destinationPath set on $cl"
  else
    fail "$cl has no backup.barmanObjectStore — WAL archiving not configured"
  fi
  # A ScheduledBackup exists?
  if [[ -n "$(kubectl -n "$PG_NS" get scheduledbackup.postgresql.cnpg.io -o name 2>/dev/null | head -1)" ]]; then
    pass "a CNPG ScheduledBackup exists in ns/$PG_NS"
  else
    fail "no ScheduledBackup in ns/$PG_NS — base backups won't run"
  fi
  # WAL archiving actually healthy → a first recoverability point exists.
  local frp
  frp=$(kubectl -n "$PG_NS" get "$cl" -o jsonpath='{.status.firstRecoverabilityPoint}' 2>/dev/null)
  if [[ -n "$frp" ]]; then
    pass "firstRecoverabilityPoint=$frp (a real restore target exists)"
  else
    fail "no firstRecoverabilityPoint yet — no completed backup/WAL to PITR from. Force one: kubectl -n $PG_NS create -f - (Backup CR), then re-run."
  fi
}

check_vault_autounseal() {
  hdr "Phase B.2 — Vault auto-unseal (transit/KMS seal, not Shamir)"
  local pod
  pod=$(kubectl -n "$VAULT_NS" get pods -l app.kubernetes.io/name=vault -o name 2>/dev/null | head -1)
  if [[ -z "$pod" ]]; then
    skip "no Vault pod in ns/$VAULT_NS → not deployed here"
    return
  fi
  local st
  st=$(kubectl -n "$VAULT_NS" exec "$pod" -- vault status -format=json 2>/dev/null)
  if [[ -z "$st" ]]; then
    skip "could not read 'vault status' from $pod (token/exec perms?)"
    return
  fi
  local sealed seal_type
  if have jq; then
    sealed=$(echo "$st" | jq -r '.sealed'); seal_type=$(echo "$st" | jq -r '.type // .seal_type // "unknown"')
  else
    sealed=$(echo "$st" | grep -o '"sealed"[: ]*[a-z]*' | grep -o '[a-z]*$')
    seal_type=$(echo "$st" | grep -o '"type"[: ]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
  fi
  if [[ "$sealed" == "false" ]]; then
    pass "Vault is unsealed (sealed=false)"
  else
    fail "Vault reports sealed=$sealed — auto-unseal did not take after restart"
  fi
  if [[ "$seal_type" != "shamir" && -n "$seal_type" && "$seal_type" != "unknown" ]]; then
    pass "seal type is '$seal_type' (auto-unseal active, not manual Shamir)"
  else
    fail "seal type is '$seal_type' — still Shamir/manual; vault_seal_config_hcl not in effect"
  fi
}

check_kata() {
  hdr "Phase B.3 — Kata microVMs (RuntimeClass + /dev/kvm + a pod that runs)"
  if [[ -z "$(kubectl get runtimeclass kata -o name 2>/dev/null)" ]]; then
    skip "no 'kata' RuntimeClass → Kata disabled (gVisor-only by design?)"
    return
  fi
  pass "RuntimeClass 'kata' exists"
  # /dev/kvm present on at least one node? (best-effort: check a node label or
  # rely on the test pod actually scheduling under kata).
  local testpod="kata-iac-verify"
  kubectl delete pod "$testpod" --ignore-not-found >/dev/null 2>&1
  cat <<YAML | kubectl apply -f - >/dev/null 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: $testpod
spec:
  runtimeClassName: kata
  restartPolicy: Never
  containers:
    - name: probe
      image: busybox:1.36
      command: ["sh","-c","ls -l /dev/kvm 2>/dev/null; echo kata-ok; sleep 2"]
YAML
  if kubectl wait --for=condition=Ready "pod/$testpod" --timeout=90s >/dev/null 2>&1 \
     || kubectl get "pod/$testpod" -o jsonpath='{.status.phase}' 2>/dev/null | grep -qE 'Running|Succeeded'; then
    pass "test pod scheduled + ran under runtimeClassName=kata (nested-virt works)"
  else
    local reason
    reason=$(kubectl get "pod/$testpod" -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}{" "}{.status.conditions[?(@.type=="PodScheduled")].message}' 2>/dev/null)
    fail "kata test pod never ran: ${reason:-unknown} — likely no /dev/kvm (Hetzner Cloud lacks nested virt; needs dedicated/metal)"
  fi
  if [[ "$KEEP_KATA_POD" -eq 0 ]]; then
    kubectl delete pod "$testpod" --ignore-not-found >/dev/null 2>&1
  fi
}

phase_b() {
  hdr "Phase B — runtime (live cluster)"
  if ! have kubectl; then
    skip "kubectl not installed → cannot run any runtime check"
    return
  fi
  if ! kubectl_ok; then
    skip "kubectl can't reach a cluster (KUBECONFIG/context?) → runtime checks skipped"
    return
  fi
  check_pitr
  check_vault_autounseal
  check_kata
}

echo -e "${C_B}Real-cloud IaC verification — env=$ENV_NAME${C_X}"
if [[ "$SKIP_TF" -eq 0 ]]; then phase_a; else hdr "Phase A skipped (--skip-terraform)"; fi
if [[ "$SKIP_RUNTIME" -eq 0 ]]; then phase_b; else hdr "Phase B skipped (--skip-runtime)"; fi

echo -e "\n${C_B}== Summary ==${C_X}  ${C_G}$PASS pass${C_X}  ${C_R}$FAIL fail${C_X}  ${C_Y}$SKIP skip${C_X}"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
