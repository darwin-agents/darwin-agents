# Darwin Agent — common tasks (run `make` or `make help`).
# Requires: Python 3.12+, uv, git-cliff (changelog), docker/compose (stack), helm (chart).

SHELL := /bin/bash
.DEFAULT_GOAL := help

UV              := uv
RUFF_PATHS      := src tests tools
DB              ?= state.db
COMPOSE         := docker compose -f docker/docker-compose.yml
HELM_CHART      := deploy/helm/darwin-platform
POSTGRES_DSN    ?= postgresql://darwin:test@127.0.0.1:5432/darwin
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
serve: install-serve ## Run supervisor on SQLite (default: state.db)
	$(UV) run darwin-platform serve --db $(DB)

serve-postgres: install-serve ## Run supervisor against POSTGRES_DSN
	test -n "$(POSTGRES_DSN)"
	$(UV) run darwin-platform serve --db "$(POSTGRES_DSN)"

# --- Docker Compose stack -----------------------------------------------------

.PHONY: docker-env docker-up docker-down docker-logs docker-build docker-ps
docker-env: ## Copy .env.example → docker/.env
	@test -f docker/.env || cp .env.example docker/.env
	@echo "Edit docker/.env for LLM provider keys (optional)."

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

test-postgres: install-all ## Postgres persistence tests (needs Postgres on :5432)
	DARWIN_POSTGRES_TEST_DSN="$(POSTGRES_DSN)" \
		$(UV) run pytest tests/unit/darwin_platform/runtime/test_persistence_postgres.py -v

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

release: ## Cut release: CHANGELOG + pyproject + commit + tag + push (VERSION=0.0.2)
	@test -n "$(VERSION)" || { echo "Usage: make release VERSION=0.0.2"; exit 1; }
	$(UV) run python scripts/changelog.py release $(VERSION) \
		$(if $(NO_TAG),--no-tag,) $(if $(NO_BUMP),--no-bump,) \
		$(if $(NO_COMMIT),--no-commit,) $(if $(NO_PUSH),--no-push,)

# --- Helm / container ---------------------------------------------------------

.PHONY: helm-lint helm-template build-image
helm-lint: ## helm lint the platform chart
	helm lint $(HELM_CHART)

helm-template: ## Render chart with default values to /tmp/darwin-default.yaml
	helm template darwin $(HELM_CHART) > /tmp/darwin-default.yaml
	@echo "Wrote /tmp/darwin-default.yaml"

build-image: ## Build supervisor image (docker/Dockerfile)
	docker build -f docker/Dockerfile -t darwin-platform:local .

# --- Cleanup ------------------------------------------------------------------

.PHONY: clean
clean: ## Remove pytest/ruff caches and local SQLite dev DB
	rm -rf .pytest_cache .ruff_cache .hypothesis
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -f $(DB) soak.db
