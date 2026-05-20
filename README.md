# Darwin Agent

**Self-modifying AI organisms with programmable wallets, isolated compute, and evolutionary fitness.**

A Darwin is an autonomous agent that earns money, manages its own treasury, rewrites its own code, and spawns better-adapted offspring — all under a signed, immutable constitution it cannot violate. This repository contains the entire stack: the genome (Layer 0), the platform control plane, real provider adapters, and deployment tooling.

---

## How it works

```
┌──────────────────────────── PLATFORM (multi-tenant SaaS) ─────────────────────────────┐
│                                                                                       │
│            Web app · Auth · Billing · Notifications                                   │
│                                │                                                      │
│          ┌─────────────────────▼───────────────────┐                                  │
│          │  Darwin Supervisor (control plane)      │                                  │
│          │  spawn · kill · pause · quota · anomaly │                                  │
│          └─────────────────────┬───────────────────┘                                  │
└────────────────────────────────┼──────────────────────────────────────────────────────┘
                                 │ provisions / supervises N
                                 ▼
   ┌─────────────────── ONE DARWIN ORGANISM (per user, fully isolated) ─────────────────┐
   │                                                                                    │
   │  ┌────────────── DNA CORE — immutable, signed (this repo: darwin_dna) ───────────┐ │
   │  │  constitution · ethics · evolution loop · kill-switch · treasury policy       │ │
   │  │  Layer N   ← sealed lessons from generation N                                 │ │
   │  │  Layer 0   ← founding genome                                                  │ │
   │  └──────────────────────────────────┬────────────────────────────────────────────┘ │
   │                                     │ read-only                                    │
   │  ┌──────────────── SOMA (mutable, sandboxed) ────────────────────────────────────┐ │
   │  │  Orchestrator (LangGraph) → Strategy → Self-modification → Escalation         │ │
   │  └──────────────────────────────────┬────────────────────────────────────────────┘ │
   │                                     │ dispatches                                   │
   │  ┌──────────────── NEURONS (specialised workers) ───────────────────────────────┐  │
   │  │  content · browser · payments · ads · trading · analytics · …                │  │
   │  └──────────────────────────────────┬───────────────────────────────────────────┘  │
   │                                     │                                              │
   │  Treasury (USDC wallet · x402) · Vault (secrets · KYC) · Audit log                 │
   └────────────────────────────────────────────────────────────────────────────────────┘
                                         │ outbound via gateways
                              LLM · Web (Browserbase) · Payment rails · APIs
```

**Two layers, one purpose:**

- **Platform** — boring, multi-tenant SaaS. Provisions and supervises organisms. Enforces platform-wide rules. Never makes business decisions for the organisms it hosts.
- **Organism** — each Darwin runs inside the platform on behalf of one user. Own treasury, own vault, own audit log, own compute. Organisms cannot talk to each other (network + auth isolation enforced at the infrastructure layer).

```mermaid
flowchart TB
    subgraph PLATFORM["🏗️ Darwin Platform  (multi-tenant SaaS)"]
        direction TB
        WEB["Web app\n(Next.js)"]
        AUTH["Auth & KYC"]
        BILLING["Billing &\nTreasury ledger"]
        NOTIF["Notification bus\n(push / email)"]

        WEB & AUTH & BILLING & NOTIF --> SUP

        SUP["Darwin Supervisor\nspawn · kill · pause · resume\nquota · anomaly detection\nescalation router"]
    end

    SUP -->|"provisions & supervises N isolated organisms"| DARWIN

    subgraph DARWIN["🧬 Darwin Organism  (one per user, fully isolated)"]
        direction TB

        subgraph DNA["DNA Core — immutable, signed  (darwin_dna)"]
            direction LR
            CONST["Constitution\n& ethics"]
            MANIFEST["Manifest +\nSignature\n(Ed25519)"]
            KILL["Kill switch"]
            AUDIT["Audit logger\n(write-before-act)"]
            TREASURY_POL["Treasury\npolicy"]
            LAYERS["Layer N ← gen N lessons\n…\nLayer 0 ← founding genome"]
        end

        DNA -->|"read-only"| SOMA

        subgraph SOMA["Soma — mutable, sandboxed  (E2B MicroVMs)"]
            ORCH["Orchestrator\n(LangGraph)"]
            STRATEGY["Strategy\nengine"]
            SELFMOD["Self-modification\nengine"]
            ESCMGR["Escalation\nmanager"]
            ORCH --> STRATEGY & SELFMOD & ESCMGR
        end

        SOMA -->|"dispatches tasks"| NEURONS

        subgraph NEURONS["Neurons — specialised workers"]
            direction LR
            N1["content\n.writer"]
            N2["browser\n.operator"]
            N3["payments\n.facilitator"]
            N4["accounts\n.manager"]
            N5["analytics\n.reader"]
            N6["…"]
        end

        NEURONS --> INFRA

        subgraph INFRA["Per-Darwin State"]
            WALLET["Treasury\n(USDC wallet · x402)"]
            VAULT["Vault\n(secrets · KYC · creds)"]
            AUDITLOG["Audit log\n(hash-chained)"]
            CHECKPOINT["LangGraph\ncheckpoint (DB)"]
        end
    end

    NEURONS -->|"outbound via gateways"| EXT

    subgraph EXT["External World"]
        direction LR
        LLM["LLM gateway\n(LiteLLM proxy)"]
        BROWSER_EXT["Browser\n(Browserbase)"]
        PAYMENTS_EXT["Payment rails\n(USDC · x402 · Stripe)"]
        APIS["APIs\n(ads · blogs · …)"]
    end

    subgraph ADAPTERS["darwin_platform/adapters"]
        direction LR
        A1["Web3ChainClient\n+ RealWallet"]
        A2["HvacVaultClient\n+ RealVaultBackend"]
        A3["BrowserUseClient"]
        A4["X402PaymentClient"]
        A5["E2BSandbox"]
        A6["LiteLLMTransport"]
    end

    SUP -.->|"wires at spawn time"| ADAPTERS
    ADAPTERS -.-> WALLET & VAULT & BROWSER_EXT & PAYMENTS_EXT & LLM

    style PLATFORM fill:#1a1a2e,stroke:#4a4a8a,color:#e0e0ff
    style DARWIN fill:#0d2137,stroke:#1a6b8a,color:#e0f0ff
    style DNA fill:#0a1f0a,stroke:#2a6b2a,color:#d0ffd0
    style SOMA fill:#1f1a0a,stroke:#7a6b1a,color:#fff0d0
    style NEURONS fill:#1f0a0a,stroke:#8a2a2a,color:#ffd0d0
    style INFRA fill:#0a0a2f,stroke:#2a2a8a,color:#d0d0ff
    style EXT fill:#1a1a1a,stroke:#555,color:#ddd
    style ADAPTERS fill:#1a0a1f,stroke:#6a2a8a,color:#f0d0ff
```

---

## Repository layout

```

src/
darwin_dna/ Layer 0 genome — signed, immutable Python package
constitution/ Immutable rule set, signed at build time
evolution/ Main loop, lesson distillation, reproduction
neurons/ Neuron contract + built-in specialised workers
machinery/ Kill switch, audit logger, treasury policy
manifest.py SHA-256 + Ed25519 trust root
signature.py Layer signing and verification

darwin_platform/ Platform control plane — FastAPI supervisor
supervisor.py Spawn / monitor / kill Darwin instances
api.py REST surface: /darwins, /healthz, /readyz, /metrics
leadership.py HA leader election (Postgres advisory locks)
persistence.py Dual-backend: SQLite file path or postgres:// DSN
billing.py Per-Darwin USDC billing
auth.py Bearer-token auth (owner, platform_operator scopes)
escalation.py Owner-approval tickets for sensitive vault reads
monitoring.py Prometheus metrics + alert counters
tracing.py OpenTelemetry / OTLP (no-op shim when absent)
adapters/ Real provider implementations (see below)

darwin_fakes/ In-memory fakes — deterministic, no network, no keys

tests/ Unit · property · integration · simulation
tools/sim/ 90-day simulated world — runs Darwin and asserts invariants
docker/ Compose stack: supervisor + Postgres + LiteLLM + Prometheus + Jaeger
deploy/helm/ Kubernetes Helm chart
docs/engineering/ Architecture and design docs (00–19)

```

---

## Quickstart

**Prerequisites:** Python 3.12+, [uv](https://github.com/astral-sh/uv)

### 1 — Install and run the tests

```bash
git clone https://github.com/quantium-rock/darwin-agent
cd darwin-agent

uv sync --extra dev
uv run pytest -q                       # unit + property + integration (~60 s)
uv run pytest -m slow -q               # 90-day simulation (~5 min)
```

### 2 — Watch a Darwin live (simulation)

```bash
uv run python -m tools.sim.run_sim --days 90
```

Runs a Darwin for 90 simulated days with in-memory fakes. No API keys needed.
Prints a live ledger, neuron dispatch log, and fitness trajectory.

### 3 — Local dev stack (Docker Compose)

Supervisor on `:8000`, Postgres, LiteLLM proxy, Prometheus on `:9090`, Jaeger on `:16686`:

```bash
cp docker/.env.example docker/.env   # add LLM provider keys (optional)
docker compose up --build
```

Full instructions: [`docker/README.md`](docker/README.md)

### 4 — Platform CLI (bare metal)

```bash
pip install -e ".[platform,postgres]"

# Dev (SQLite, no auth):
darwin-platform serve --db state.db

# Production (Postgres, HA leader election):
darwin-platform serve \
  --db postgresql://darwin:pass@localhost:5432/darwin \
  --leader-election \
  --auth-tokens /run/secrets/tokens.json

# Production with all real adapters:
darwin-platform serve \
  --db postgresql://darwin:pass@localhost:5432/darwin \
  --leader-election \
  --rpc-url https://sepolia.base.org \
  --wallet-key 0xYOUR_PRIVATE_KEY \
  --vault-url https://vault.example.com:8200 \
  --vault-token s.abc \
  --browserbase-api-key bb-key-123 \
  --e2b-api-key e2b-key-456
```

---

## Key concepts

### Genome (Layer 0)

The `darwin_dna` package is the **founding genome** — signed with Ed25519, verified on every boot. It contains the rules of the game. It can never be changed from inside a running Darwin; only the platform can issue a new signed layer after human review.

Four subsystems are tested with paranoia (malicious soma assumed):

| Subsystem                | Role                                                                                                                                         |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| **Signature + Manifest** | SHA-256 + Ed25519 trust root. Every mutation is verified before load.                                                                        |
| **Kill switch**          | Graded: pause → quarantine → kill. Polls at every loop iteration and neuron dispatch. Max latency from flag-flip to halt < 2 s.              |
| **Audit logger**         | Write-before-act, append-only, hash-chained. Every action is anchored before execution. Permanent — never deleted even after organism death. |
| **Treasury policy**      | Hard caps + owner soft caps. Blocks _before_ a spend, not after.                                                                             |

### Neurons

Neurons are specialised sub-agents dispatched by the orchestrator. Each gets a `NeuronContext` scoped to exactly the capabilities it needs — no neuron sees the genome, the treasury key, or another neuron's state:

```
content.writer      browser.operator    payments.facilitator
accounts.manager    ads.manager         analytics.reader  …
```

Neurons with `web.operate` / `web.publish` tags automatically receive an x402 HTTP client so HTTP 402 responses are settled against the Darwin's wallet transparently.

### Evolution

Each Darwin distills its lessons into a new signed DNA layer and optionally spawns a child with the accumulated genome. The parent dies; the child inherits everything the parent learned. This is the selection pressure that drives fitness across generations.

### Real adapters vs. fakes

Every external dependency ships as a Protocol + in-memory fake (used by default in tests and the sim) and a real implementation wired via factory functions in the supervisor. Switch between them with CLI flags or env vars — no code changes needed.

| Capability      | Fake          | Real adapter                           | Extra        |
| --------------- | ------------- | -------------------------------------- | ------------ |
| LLM gateway     | `FakeLLM`     | `LiteLLMTransport`                     | `[llm]`      |
| Wallet / chain  | `FakeWallet`  | `RealWallet` + `Web3ChainClient`       | `[web3]`     |
| Secret vault    | `FakeVault`   | `RealVaultBackend` + `HvacVaultClient` | `[hvac]`     |
| Browser         | `FakeBrowser` | `BrowserUseClient` (Browserbase)       | `[browser]`  |
| MicroVM sandbox | `FakeSandbox` | `E2BSandbox`                           | `[e2b]`      |
| HTTP / x402     | none          | `X402PaymentClient`                    | `[platform]` |

---

## API surface

```
POST   /darwins                              spawn a new Darwin
GET    /darwins/{id}                         status + treasury snapshot
POST   /darwins/{id}/pause                   pause (reversible)
POST   /darwins/{id}/resume                  resume after pause
POST   /darwins/{id}/kill                    terminate (irreversible)
POST   /darwins/{id}/reproduce/force         trigger reproduction ahead of schedule
POST   /darwins/{id}/escalations/{eid}/respond  owner responds to an escalation ticket
GET    /darwins/{id}/lineage                 ancestry tree
GET    /darwins/{id}/ledger                  financial ledger
GET    /darwins/{id}/events                  event log (paginated)

GET    /healthz                              liveness probe
GET    /readyz                               readiness probe (503 on HA followers)
GET    /metrics                              Prometheus metrics
```

---

## Environment variables

All real adapters fall back to in-memory fakes when their env vars are absent.

| Variable              | Purpose                                                      |
| --------------------- | ------------------------------------------------------------ |
| `DARWIN_RPC_URL`      | EVM HTTP RPC endpoint (e.g. `https://sepolia.base.org`)      |
| `DARWIN_WALLET_KEY`   | Hex private key for on-chain USDC signing — **never commit** |
| `DARWIN_VAULT_URL`    | HashiCorp Vault address                                      |
| `DARWIN_VAULT_TOKEN`  | Vault token with KV v2 read+write                            |
| `BROWSERBASE_API_KEY` | Browserbase API key for browser automation                   |
| `BROWSERBASE_URL`     | Browserbase WebSocket endpoint (alternative to API key)      |
| `E2B_API_KEY`         | E2B API key for MicroVM sandbox sessions                     |

---

## Install extras

```bash
pip install -e ".[platform]"   # FastAPI + OTel — required to serve the API
pip install -e ".[postgres]"   # psycopg 3 — Postgres persistence + HA
pip install -e ".[web3]"       # web3.py — real wallet adapter
pip install -e ".[hvac]"       # hvac — HashiCorp Vault adapter
pip install -e ".[browser]"    # browser-use — browser substrate
pip install -e ".[e2b]"        # e2b — MicroVM sandbox
pip install -e ".[graph]"      # langgraph — orchestrator graph
pip install -e ".[llm]"        # litellm + httpx — LLM gateway client
pip install -e ".[dev]"        # pytest + ruff — development tooling
```

---

## Deployment

### Docker Compose (dev / staging)

See [`docker/README.md`](docker/README.md).

### Kubernetes (Helm)

See [`deploy/helm/darwin-platform/README.md`](deploy/helm/darwin-platform/README.md) for the full values reference, HA setup, real-adapter secret injection, and production checklist.

```bash
# HA production install:
helm install darwin deploy/helm/darwin-platform \
  --namespace darwin --create-namespace \
  --set image.tag=sha-abc1234 \
  --set replicaCount=3 \
  --set leaderElection.enabled=true \
  --set database.existingSecret=darwin-db-url \
  --set auth.enabled=true \
  --set auth.existingSecret=darwin-platform-tokens \
  --set serviceMonitor.enabled=true \
  --set ingress.enabled=true \
  --set ingress.className=nginx
```

---

## Design docs

Full engineering documentation is in [`docs/engineering/`](docs/engineering/).

| Doc                                                                                              | Description                                                                                                             |
| ------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| [01 — System overview](docs/engineering/01-system-overview.md)                                   | Platform vs. organism: the two-layer model, isolation rationale, and the high-level architecture diagram.               |
| [02 — DNA core](docs/engineering/02-dna-core.md)                                                 | What the immutable genome contains, how layers are loaded and verified, and the layer-stack structure.                  |
| [03 — Soma and orchestrator](docs/engineering/03-soma-and-orchestrator.md)                       | The mutable body: LangGraph workflow, strategy engine, self-modification loop, and escalation manager.                  |
| [04 — Neurons](docs/engineering/04-neurons.md)                                                   | Neuron contract, the built-in catalog (content, browser, payments, ads, …), and how new neurons are added.              |
| [05 — Treasury and payments](docs/engineering/05-treasury-and-payments.md)                       | Per-Darwin USDC wallet, x402 programmable spend rules, ledger design, and what happens at treasury-zero.                |
| [06 — Evolution and reproduction](docs/engineering/06-evolution-and-reproduction.md)             | Fitness measurement, lesson distillation, mutation, selection pressure, and the reproduction event.                     |
| [07 — Isolation and multi-tenancy](docs/engineering/07-isolation-and-multi-tenancy.md)           | Network, compute (MicroVMs), data, and identity isolation; the threat model and blast-radius analysis.                  |
| [08 — Platform control plane](docs/engineering/08-platform-control-plane.md)                     | Supervisor API, escalation router, anomaly detection, kill-switch propagation, billing model, and the web app canvases. |
| [09 — Kill switches and owner controls](docs/engineering/09-kill-switches-and-owner-controls.md) | Three kill triggers (owner, platform, self), latencies, network-cut mechanics, and the owner control surface.           |
| [10 — Alignment and anti-wireheading](docs/engineering/10-alignment-and-anti-wireheading.md)     | Agent-safety concerns specific to Darwin: pathological optimization resistance, monitoring approach, and known gaps.    |
| [11 — Tech stack at a glance](docs/engineering/11-tech-stack-at-a-glance.md)                     | Every technology choice on one page — what was picked, what was considered, and why.                                    |
| [12 — Orchestration — LangGraph](docs/engineering/12-orchestration-langgraph.md)                 | Why LangGraph, how the main loop and self-modification engine are structured, and checkpointing design.                 |
| [13 — LLM routing — LiteLLM](docs/engineering/13-llm-routing-litellm.md)                         | Self-hosted LiteLLM proxy: tiered routing (cheap → expensive), cost attribution per Darwin, and fallback strategy.      |
| [14 — Sandboxing — E2B](docs/engineering/14-sandboxing-e2b.md)                                   | Firecracker MicroVM sandbox (E2B primary, Modal escape hatch): execution isolation and session lifecycle.               |
| [15 — Browser automation stack](docs/engineering/15-browser-stack.md)                            | Browser-Use on Browserbase: autonomous page interaction, stealth proxies, session recording, and cost model.            |
| [16 — Secrets and vault](docs/engineering/16-secrets-and-vault.md)                               | HashiCorp Vault with per-Darwin namespaces: bidirectional user+Darwin access, KYC doc storage, and tiered read grants.  |
| [17 — Payments providers](docs/engineering/17-payments-providers.md)                             | Concrete provider choices: Coinbase Agentic Wallets, x402 protocol, Stripe fiat ramps, and AgentCore Payments.          |
| [18 — Layer 0 build plan](docs/engineering/18-layer-zero-build-plan.md)                          | Engineering build plan for the founding genome: file layout, module sketches, package decisions, and testing strategy.  |

---

## Boot guarantee

Layer 0 boots a Darwin into a _working_ state on first run — naive and unprofitable is acceptable, non-functional is not. The loop runs, neurons dispatch, the ledger reconciles, the kill switch halts, the audit chain is unbroken. Evolution improves everything above that floor; nothing falls below it.
