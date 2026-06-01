#!/usr/bin/env bash
# Preflight for `make real` — verifies the environment is ready to run the
# platform against real LLM/wallet/browser/sandbox backends. Exits non-zero
# (and prints what's missing) if the configuration would silently fall back
# to a fake. Read-only: makes no network calls, mutates no files.
#
# Three knobs:
#   --network testnet|mainnet   Default: testnet. mainnet adds CDP-mainnet
#                               check + refuses if the RPC URL still says
#                               "sepolia".
#   --tokens PATH               Path to the auth-tokens JSON file. Default:
#                               var/tokens.json (gitignored). Created from a
#                               template if missing.
#   --strict                    Treat optional integrations (Slack, FCM,
#                               PagerDuty, Vault, Stripe, KYC) as required.
set -euo pipefail

NETWORK="testnet"
TOKENS_FILE="var/tokens.json"
STRICT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --network) NETWORK="$2"; shift 2;;
    --tokens)  TOKENS_FILE="$2"; shift 2;;
    --strict)  STRICT=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

if [[ "$NETWORK" != "testnet" && "$NETWORK" != "mainnet" ]]; then
  echo "--network must be testnet or mainnet" >&2
  exit 2
fi

C_R='\033[0;31m'
C_G='\033[0;32m'
C_Y='\033[0;33m'
C_X='\033[0m'

fail=0
warn=0

check_required() {
  local var="$1" detail="$2"
  if [[ -z "${!var:-}" ]]; then
    printf "${C_R}MISSING${C_X}  %-32s %s\n" "$var" "$detail"
    fail=$((fail+1))
  else
    printf "${C_G}OK     ${C_X}  %-32s set\n" "$var"
  fi
}

check_optional() {
  local var="$1" detail="$2"
  if [[ -z "${!var:-}" ]]; then
    if [[ $STRICT -eq 1 ]]; then
      printf "${C_R}MISSING${C_X}  %-32s %s\n" "$var" "$detail"
      fail=$((fail+1))
    else
      printf "${C_Y}skip   ${C_X}  %-32s %s\n" "$var" "$detail"
      warn=$((warn+1))
    fi
  else
    printf "${C_G}OK     ${C_X}  %-32s set\n" "$var"
  fi
}

echo
echo "=== Darwin platform preflight (${NETWORK}) ==="
echo

echo "-- LLM providers (at least one is required) --"
have_llm=0
for v in CLAUDE_API_KEY OPENAI_API_KEY GEMINI_API_KEY; do
  if [[ -n "${!v:-}" ]]; then
    have_llm=$((have_llm+1))
    printf "${C_G}OK     ${C_X}  %-32s set\n" "$v"
  else
    printf "${C_Y}skip   ${C_X}  %-32s (not required, but at least one LLM key must be set)\n" "$v"
  fi
done
if [[ $have_llm -eq 0 ]]; then
  printf "${C_R}MISSING${C_X}  %-32s no LLM provider configured\n" "CLAUDE_API_KEY"
  fail=$((fail+1))
fi
echo

echo "-- Wallet (Coinbase CDP — default production provider) --"
check_required CDP_API_KEY_ID     "Coinbase Developer Platform API key id"
check_required CDP_API_KEY_SECRET "Coinbase Developer Platform API secret"
check_required CDP_WALLET_SECRET  "CDP wallet-signing secret (separate from API)"
echo

echo "-- Browser substrate --"
check_required BROWSERBASE_URL     "https://api.browserbase.com (or self-hosted)"
check_required BROWSERBASE_API_KEY "browser.operator runtime"
echo

echo "-- Sandbox substrate --"
check_required E2B_API_KEY "E2B microVM credentials"
echo

echo "-- Persistence --"
if [[ -n "${DATABASE_URL:-}" ]]; then
  printf "${C_G}OK     ${C_X}  %-32s Postgres configured\n" "DATABASE_URL"
else
  printf "${C_Y}skip   ${C_X}  %-32s falling back to SQLite (state.db); set for HA\n" "DATABASE_URL"
  warn=$((warn+1))
fi
echo

echo "-- Auth tokens --"
if [[ -f "$TOKENS_FILE" ]]; then
  if grep -q '"token"' "$TOKENS_FILE" 2>/dev/null; then
    printf "${C_G}OK     ${C_X}  %-32s %s exists\n" "auth-tokens" "$TOKENS_FILE"
  else
    printf "${C_R}MISSING${C_X}  %-32s %s exists but has no tokens\n" "auth-tokens" "$TOKENS_FILE"
    fail=$((fail+1))
  fi
else
  printf "${C_Y}create ${C_X}  %-32s writing template at %s\n" "auth-tokens" "$TOKENS_FILE"
  mkdir -p "$(dirname "$TOKENS_FILE")"
  cat > "$TOKENS_FILE" <<'JSON'
{
  "tokens": [
    {
      "token": "REPLACE-ME-WITH-A-RANDOM-STRING",
      "subject": "you@example.com",
      "scopes": ["owner", "operator"],
      "owned_darwins": ["*"]
    }
  ]
}
JSON
  printf "${C_R}ACTION ${C_X}  edit %s to set a real token before booting\n" "$TOKENS_FILE"
  fail=$((fail+1))
fi
echo

echo "-- Optional integrations (silently fake-fall-back unless --strict) --"
check_optional DARWIN_SLACK_WEBHOOK_URL       "owner Slack notifications"
check_optional DARWIN_PAGERDUTY_ROUTING_KEY   "critical alerts page on-call"
check_optional DARWIN_FCM_PROJECT_ID          "mobile push to operator app"
check_optional GOOGLE_APPLICATION_CREDENTIALS "FCM service-account JSON path"
check_optional DARWIN_VAULT_URL               "HashiCorp Vault for sensitive secrets"
check_optional DARWIN_VAULT_TOKEN             "Vault auth token"
check_optional STRIPE_API_KEY                 "fiat-ramp deposits + Connect withdrawals"
check_optional DARWIN_SANCTIONS_API_KEY       "Chainalysis / TRM screening"
check_optional DARWIN_KYC_PERSONA_API_KEY     "Persona KYC adapter"
echo

if [[ "$NETWORK" == "mainnet" ]]; then
  echo "-- Mainnet sanity checks (real money — hard requirements) --"
  # 1. No testnet RPC URL leaking into a mainnet boot.
  for var in DARWIN_RPC_URL DARWIN_SOLANA_RPC_URL DARWIN_STELLAR_HORIZON_URL; do
    url="${!var:-}"
    if [[ "$url" == *sepolia* || "$url" == *goerli* || "$url" == *testnet* \
          || "$url" == *devnet* || "$url" == *horizon-testnet* ]]; then
      printf "${C_R}MISMATCH${C_X} %-22s %s looks like testnet under --network mainnet\n" "$var" "$url"
      fail=$((fail+1))
    elif [[ -n "$url" ]]; then
      printf "${C_G}OK     ${C_X}  %-32s %s\n" "$var" "$url"
    fi
  done
  # 2. Live sanctions screening is mandatory (the supervisor refuses to
  #    boot on mainnet without it — keep preflight in lockstep).
  if [[ -z "${DARWIN_SANCTIONS_API_KEY:-}" ]]; then
    printf "${C_R}MISSING${C_X}  %-32s mainnet needs a live screener (Chainalysis/TRM)\n" "DARWIN_SANCTIONS_API_KEY"
    fail=$((fail+1))
  else
    printf "${C_G}OK     ${C_X}  %-32s set (pass --sanctions-provider chainalysis|trm)\n" "DARWIN_SANCTIONS_API_KEY"
  fi
  # 3. No test-grade Stripe key on mainnet.
  if [[ "${STRIPE_API_KEY:-}" == sk_test_* ]]; then
    printf "${C_R}TESTKEY ${C_X} %-32s sk_test_ key on mainnet — use a live sk_live_ key\n" "STRIPE_API_KEY"
    fail=$((fail+1))
  fi
  # 4. KYC should be a real provider, not the placeholder (warn — only
  #    strictly needed for fiat / withdrawals).
  if [[ "${DARWIN_KYC_PERSONA_API_KEY:-}" == "persona_..." \
        || -z "${DARWIN_KYC_PERSONA_API_KEY:-}${ONFIDO_API_TOKEN:-}" ]]; then
    printf "${C_Y}warn   ${C_X}  %-32s no real KYC provider (needed for fiat/withdrawals)\n" "KYC"
    warn=$((warn+1))
  fi
  # 5. Vault should use a real token (unset / root token is dev-grade).
  if [[ -z "${DARWIN_VAULT_TOKEN:-}" ]]; then
    printf "${C_Y}warn   ${C_X}  %-32s no Vault token — prod should use AppRole/k8s-auth\n" "DARWIN_VAULT_TOKEN"
    warn=$((warn+1))
  fi
  echo
fi

echo "-- Summary --"
if [[ $fail -gt 0 ]]; then
  printf "${C_R}FAIL${C_X} %d missing required, %d optional skipped\n" "$fail" "$warn"
  echo "  fix the MISSING items in your .env (or pass --strict=false to skip optional ones)."
  exit 1
fi
printf "${C_G}PASS${C_X} all required env vars set; %d optional integrations skipped\n" "$warn"
echo "next: make real-serve     # boot the platform"
echo "      make real-checklist  # smoke test against the running platform"
