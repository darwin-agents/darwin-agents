# Darwin Agent — common tasks (run `make` or `make help`).
# Requires: Python 3.12+, uv, git-cliff (changelog), docker/compose (stack), helm (chart).

SHELL := /bin/bash
.DEFAULT_GOAL := help

UV              := uv
RUFF_PATHS      := src tests tools
DB              ?= var/state.db
COMPOSE         := docker compose -f infra/docker/docker-compose.yml --env-file .env
HELM_CHART      := infra/charts/darwin-platform
POSTGRES_DSN    ?= postgresql://darwin:test@127.0.0.1:5432/darwin
# Origins allowed to call the platform from a browser. Space-separated.
# Override: `make serve CORS_ORIGINS="http://localhost:3000 https://app.example.com"`.
CORS_ORIGINS    ?= http://localhost:3000
SIM_DAYS        ?= 90
GIT_CLIFF       := git-cliff
CHANGELOG       := CHANGELOG.md

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z0-9_.-]+:.*##' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- Python / install ---------------------------------------------------------

.PHONY: python install install-all install-serve lock
python: ## Install Python 3.12 via uv
	$(UV) python install 3.12

install: python ## Minimal dev deps (pytest, ruff)
	$(UV) sync --extra dev

install-all: python ## All extras + dev (matches CI)
	$(UV) sync --all-extras --dev

install-serve: python ## Dev + platform + postgres for `make serve`
	$(UV) sync --extra dev --extra platform --extra postgres

lock: ## Refresh uv.lock after pyproject changes
	$(UV) lock

# --- Local platform (bare metal) ----------------------------------------------

.PHONY: serve serve-postgres
# CORS_ORIGINS is space-separated; map it to repeated --cors-origin flags.
CORS_FLAGS = $(foreach o,$(CORS_ORIGINS),--cors-origin $(o))

serve: install-serve ## Run supervisor on SQLite (default: var/state.db)
	@mkdir -p $(dir $(DB))
	$(UV) run darwin-platform serve --db $(DB) $(CORS_FLAGS)

serve-postgres: install-serve ## Run supervisor against POSTGRES_DSN
	test -n "$(POSTGRES_DSN)"
	$(UV) run darwin-platform serve --db "$(POSTGRES_DSN)" $(CORS_FLAGS)

# --- Real (skin-in-the-game) -------------------------------------------------
# `make real` is the end-to-end Stage-1 recipe: preflight env, then boot the
# platform against real LLM keys + CDP wallet + Browserbase + E2B. Defaults
# to testnet (Base Sepolia); pass NETWORK=mainnet for production.
#
# Env vars are loaded from .env automatically. Override DB / TOKENS via
# CLI: `make real TOKENS=path/to/tokens.json DB=prod.db NETWORK=mainnet`.

REAL_TOKENS  ?= var/tokens.json
REAL_NETWORK ?= testnet
REAL_OPENING_USDC ?= 1.0

# Build the list of real-adapter flags. The CLI gracefully falls back to a
# fake when a flag is omitted; we pass them explicitly so a missing env var
# fails LOUD instead of silently using a fake in a "real" boot.
REAL_FLAGS = \
  --wallet-provider cdp \
  --browser-provider browserbase \
  --network $(REAL_NETWORK) \
  $(if $(DARWIN_VAULT_URL),--vault-url $(DARWIN_VAULT_URL),) \
  $(if $(DARWIN_VAULT_TOKEN),--vault-token $(DARWIN_VAULT_TOKEN),) \
  $(if $(DARWIN_SANCTIONS_API_KEY),--sanctions-provider chainalysis --sanctions-api-key $(DARWIN_SANCTIONS_API_KEY),) \
  $(if $(STRIPE_API_KEY),--fiat-provider stripe,) \
  $(if $(and $(DARWIN_KYC_PERSONA_API_KEY),$(DARWIN_KYC_PERSONA_TEMPLATE_ID)),--kyc-provider persona --kyc-persona-api-key $(DARWIN_KYC_PERSONA_API_KEY) --kyc-persona-template-id $(DARWIN_KYC_PERSONA_TEMPLATE_ID),) \
  $(if $(DARWIN_LITELLM_URL),--litellm-url $(DARWIN_LITELLM_URL) --litellm-master-key $(DARWIN_LITELLM_MASTER_KEY),) \
  $(if $(OTEL_EXPORTER_OTLP_ENDPOINT),--otlp-endpoint $(OTEL_EXPORTER_OTLP_ENDPOINT),) \
  $(if $(DARWIN_ESCALATION_WEBHOOK_URL),--escalation-webhook-url $(DARWIN_ESCALATION_WEBHOOK_URL),) \
  $(if $(DARWIN_ALERT_WEBHOOK_URL),--alert-webhook-url $(DARWIN_ALERT_WEBHOOK_URL),) \
  $(if $(DARWIN_SLACK_WEBHOOK_URL),--slack-webhook-url $(DARWIN_SLACK_WEBHOOK_URL),) \
  $(if $(DARWIN_PAGERDUTY_ROUTING_KEY),--pagerduty-routing-key $(DARWIN_PAGERDUTY_ROUTING_KEY),) \
  $(if $(DARWIN_WALLET_KEY),--enable-omnibus,)
# Note: E2B is picked up via E2B_API_KEY in the environment (no CLI flag).
# Browserbase reads BROWSERBASE_URL + BROWSERBASE_API_KEY from env when the
# --browser-provider flag is set above.
# Omnibus: --enable-omnibus turns on when DARWIN_WALLET_KEY is set; it reads
# DARWIN_RPC_URL + DARWIN_WALLET_KEY for its own web3 wallet. Per-Darwin
# wallets stay on CDP (the web3 shared-key path is gated off under cdp).

.PHONY: real real-preflight real-serve real-checklist real-spawn
real: real-preflight real-serve ## Preflight + boot platform against real LLM/wallet/browser/sandbox

real-preflight: ## Validate .env has the credentials a real boot needs
	@test -f .env || { echo "missing .env — run: cp .env.example .env && edit it" >&2; exit 1; }
	@set -a; . ./.env; set +a; \
	  scripts/real_preflight.sh --network $(REAL_NETWORK) --tokens $(REAL_TOKENS)

real-serve: install-all ## Boot platform against real adapters (after preflight)
	@test -f .env  || { echo "missing .env" >&2; exit 1; }
	@test -f $(REAL_TOKENS) || { echo "missing $(REAL_TOKENS); run: make real-preflight" >&2; exit 1; }
	@mkdir -p $(dir $(DB))
	set -a; . ./.env; set +a; \
	  $(UV) run --all-extras darwin-platform serve \
	    --db $(DB) \
	    --auth-tokens $(REAL_TOKENS) \
	    --heartbeat-seconds 5 \
	    $(CORS_FLAGS) \
	    $(REAL_FLAGS)

real-checklist: ## Smoke test the running platform (the 3 pre-mainnet items)
	@test -f $(REAL_TOKENS) || { echo "missing $(REAL_TOKENS)" >&2; exit 1; }
	@TOKEN=$$(python -c "import json,sys;d=json.load(open('$(REAL_TOKENS)'));t=d['tokens'] if isinstance(d,dict) else d;print(t[0]['token'])"); \
	$(UV) run python scripts/real_checklist.py \
	  --url http://127.0.0.1:8000 \
	  --token "$$TOKEN" \
	  --opening-usdc $(REAL_OPENING_USDC)

real-spawn: ## Spawn one Darwin against the running platform (uses first token)
	@test -f $(REAL_TOKENS) || { echo "missing $(REAL_TOKENS)" >&2; exit 1; }
	@TOKEN=$$(python -c "import json,sys;d=json.load(open('$(REAL_TOKENS)'));t=d['tokens'] if isinstance(d,dict) else d;print(t[0]['token'])"); \
	curl -sS -X POST http://127.0.0.1:8000/darwins \
	  -H "Authorization: Bearer $$TOKEN" \
	  -H "Content-Type: application/json" \
	  -H "Idempotency-Key: spawn-$$(date +%s)" \
	  -d '{"opening_usdc": $(REAL_OPENING_USDC), "escalation_auto": "approve"}' | python -m json.tool

# --- Docker Compose stack -----------------------------------------------------

.PHONY: docker-env docker-up docker-down docker-logs docker-build docker-ps
docker-env: ## Ensure repo-root .env exists for the Compose stack
	@test -f .env || cp .env.example .env
	@echo "Edit .env for LLM provider keys (optional)."

docker-up: docker-env ## Build and start supervisor + Postgres + LiteLLM + metrics
	$(COMPOSE) up --build -d

docker-down: ## Stop the Compose stack
	$(COMPOSE) down

docker-logs: ## Follow Compose logs
	$(COMPOSE) logs -f

docker-build: ## Build supervisor image only
	$(COMPOSE) build supervisor

docker-ps: ## Show Compose service status
	$(COMPOSE) ps

# --- Tests (pytest) -------------------------------------------------------------

.PHONY: test test-fast test-all test-unit test-property test-redteam \
        test-integration test-simulation test-slow test-postgres
test: test-fast ## Alias for fast test tier

test-fast: install-all ## Fast tier: unit + property + redteam + integration (~90s)
	$(UV) run pytest -m "not slow" --maxfail=3

test-all: install-all ## Full suite including simulation tier (~3–5 min)
	$(UV) run pytest

test-unit: install-all ## Unit tier only
	$(UV) run pytest tests/unit

test-property: install-all ## Property / Hypothesis tier only
	$(UV) run pytest tests/property

test-redteam: install-all ## Red-team adversarial tier only
	$(UV) run pytest tests/redteam

test-integration: install-all ## Integration tier only
	$(UV) run pytest tests/integration

test-simulation: install-all ## Simulation path (same as test-slow)
	$(UV) run pytest tests/simulation

test-slow: install-all ## Simulation marker only (CI slow workflow)
	$(UV) run pytest -m slow -v

test-postgres: install-all ## Postgres persistence + RLS + leader-election tests (needs Postgres on :5432)
	DARWIN_POSTGRES_TEST_DSN="$(POSTGRES_DSN)" \
		$(UV) run pytest -v tests/unit/darwin_platform/runtime/test_persistence_postgres.py \
		tests/unit/darwin_platform/runtime/test_persistence_rls.py \
		tests/unit/darwin_platform/runtime/test_leadership.py

# --- Lint / format ------------------------------------------------------------

.PHONY: lint format format-check check ci
lint: install ## ruff check (CI rule set)
	$(UV) run ruff check $(RUFF_PATHS)

format: install ## Auto-format with ruff
	$(UV) run ruff format $(RUFF_PATHS)

format-check: install ## Check formatting (non-blocking in CI today)
	$(UV) run ruff format --check $(RUFF_PATHS) || true

check: lint test-fast ## Pre-PR: lint + fast tests

ci: install-all lint test-fast ## Mirror main CI signal locally

# --- Tools / simulations ------------------------------------------------------

.PHONY: sim evolution soak
sim: install-all ## 90-day in-process sim (override: make sim SIM_DAYS=30)
	$(UV) run python -m tools.sim.run_sim --days $(SIM_DAYS)

evolution: install-all ## Phase 1 evolution harness (doc 21)
	$(UV) run python -m tools.evolution_test.run_evolution

soak: install-serve ## Phase 0 soak driver (platform must be running separately)
	$(UV) run python -m tools.soak.run_soak $(ARGS)

# --- Changelog (git-cliff + scripts/changelog.py) ------------------------------

.PHONY: changelog changelog-sync changelog-preview release
changelog: changelog-sync ## Alias: rewrite [Unreleased] from commits since last tag

changelog-preview: ## Print [Unreleased] to stdout (no file changes)
	$(UV) run python scripts/changelog.py preview

changelog-sync: ## Replace [Unreleased] in CHANGELOG.md from git history
	$(UV) run python scripts/changelog.py sync

release: ## Cut release: CHANGELOG + pyproject + commit + tag + push (VERSION=0.0.2 or omit for patch bump)
	$(UV) run python scripts/changelog.py release \
		$(if $(VERSION),$(VERSION),) \
		$(if $(NO_TAG),--no-tag,) $(if $(NO_BUMP),--no-bump,) \
		$(if $(NO_COMMIT),--no-commit,) $(if $(NO_PUSH),--no-push,)

# --- Helm / container ---------------------------------------------------------

.PHONY: helm-lint helm-template build-image
helm-lint: ## helm lint the platform chart
	helm lint $(HELM_CHART)

helm-template: ## Render chart with default values to /tmp/darwin-default.yaml
	helm template darwin $(HELM_CHART) > /tmp/darwin-default.yaml
	@echo "Wrote /tmp/darwin-default.yaml"

build-image: ## Build supervisor image (infra/docker/Dockerfile)
	docker build -f infra/docker/Dockerfile -t darwin-platform:local .

# --- Local k8s dev (kind, single command) -------------------------------------
# `make dev` is the turnkey local loop: it builds the supervisor image, brings
# up a kind cluster, loads the image, applies the full cloud-agnostic stack
# (Postgres + Vault + LiteLLM + supervisor + ingress via Terraform), and starts
# the dashboard on :3000 — all idempotent and re-runnable. See scripts/dev-up.sh.

.PHONY: dev dev-down dev-kubeconfig
dev: ## One command: kind + full platform stack + dashboard (localhost:3000)
	scripts/dev-up.sh

dev-down: ## Tear down the local kind stack + dashboard
	scripts/dev-down.sh

dev-kubeconfig: ## Print KUBECONFIG export for the local kind cluster
	@echo "export KUBECONFIG=$(abspath infra/terraform/envs/local/.kubeconfig)"

.PHONY: local-down
local-down: ## Tear down BOTH local stacks: kind (dev) + Compose (docker)
	-$(MAKE) dev-down
	-$(MAKE) docker-down

# --- Web dashboard (Next.js) --------------------------------------------------

WEB_DIR := web
PNPM    := pnpm

.PHONY: web-install web-dev web-build web-lint web-typecheck web-test web-codegen
web-install: ## Install web/ dependencies
	cd $(WEB_DIR) && $(PNPM) install

web-dev: ## Run Next.js dev server on :3000
	cd $(WEB_DIR) && $(PNPM) dev

web-build: ## Build the web bundle
	cd $(WEB_DIR) && $(PNPM) build

web-lint: ## ESLint + Prettier check for web/
	cd $(WEB_DIR) && $(PNPM) lint

web-typecheck: ## tsc --noEmit for web/
	cd $(WEB_DIR) && $(PNPM) typecheck

web-test: ## Vitest unit tests for web/
	cd $(WEB_DIR) && $(PNPM) test

web-codegen: ## Regenerate web/lib/api.generated.ts from a running platform
	cd $(WEB_DIR) && $(PNPM) codegen

# --- Cleanup ------------------------------------------------------------------

.PHONY: clean
clean: ## Remove pytest/ruff caches and local SQLite dev DB
	rm -rf .pytest_cache .ruff_cache .hypothesis
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -f $(DB) soak.db
