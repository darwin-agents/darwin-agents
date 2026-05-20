# Darwin Agent

**Self-modifying AI organisms with programmable wallets, isolated compute, and evolutionary fitness.**

---

## About Darwin

Darwin is a platform for launching **economically autonomous digital organisms** — not one-shot agent scripts that wake up, run a task, and disappear, but persistent workers with real stakes. Each user spawns a *Darwin* with a starting budget; the organism earns revenue through online ventures its **neurons** operate, pays its own infrastructure and API costs from a programmable **treasury**, and either stays alive or dies when capital runs out. Profitable lineages **reproduce**, sealing what worked into the next generation's DNA; unprofitable ones die. That selection pressure is the point.

The architecture splits cleanly into two layers:

| Layer | Role |
| --- | --- |
| **DNA core** (immutable) | Signed genome: constitution, kill switch, audit logger, treasury policy, evolution machinery. The running Darwin can read it but never mutate it. |
| **Soma** (mutable) | Sandboxed agent body: orchestrator, strategy, self-modification loop. All runtime code changes happen here. |
| **Neurons** | Narrow worker sub-agents (content, browser, payments, …) — each scoped to exactly the capabilities it needs. |

The biological metaphor is **load-bearing**, not decorative. Metabolism is spend; food is revenue; hunger is a falling balance; death is bankruptcy or an owner kill-switch; reproduction is how progress compounds across generations. Every design decision should be checkable against it. See [Vision and metaphor](docs/overview/01-vision-and-metaphor.md) for the full mapping and rationale.

Darwin is a bet on two propositions:

1. **Compounding code** — agents that improve their own scaffolding under economic selection pressure, not just benchmark scores (building on the Darwin Gödel Machine lineage).
2. **Owned automation** — user-owned workers that earn on the user's behalf; the platform hosts and takes a cut, the user keeps the upside.

It is explicitly **not** AGI, a get-rich-quick scheme, or fully unsupervised operation. The human owner stays in the loop for captchas, KYC, high-stakes spend, and instant kill-switch control. Most individual Darwins are expected to fail; the value is in **successful lineages** that compound. See [What Darwin is not](docs/overview/01-vision-and-metaphor.md#what-this-is-not) and [Design principles (P1–P10)](docs/overview/02-design-principles.md).

**This repository** contains the full implementation stack: the Layer 0 genome (`darwin_dna`), the platform control plane (`darwin_platform`), real provider adapters, in-memory fakes for tests, simulation tooling, and deployment charts.

**Documentation** is split into two folders — read [`docs/overview/`](docs/overview/) for the *why* and product context, then [`docs/engineering/`](docs/engineering/) for the *how*.

---

## How it works


```mermaid
flowchart TB
    subgraph PLATFORM["🏗️ Platform — multi-tenant SaaS"]
        WEB["Web app"] & AUTH["Auth / KYC"] & BILLING["Billing"] & NOTIF["Notifications"]
        WEB & AUTH & BILLING & NOTIF --> SUP["Darwin Supervisor\nspawn · kill · pause · quota · anomaly"]
    end

    SUP -->|"provisions N isolated organisms"| DARWIN

    subgraph DARWIN["🧬 Darwin Organism — one per user, fully isolated"]
        subgraph DNA["DNA Core — immutable, signed  (darwin_dna)"]
            CONST["Constitution"] & MANIFEST["Manifest\nEd25519"] & KILL["Kill\nswitch"] & AUDIT["Audit\nlogger"] & TPOL["Treasury\npolicy"] & LAYERS["Layer 0 → N\ngenome stack"]
        end

        DNA -->|"read-only"| SOMA

        subgraph SOMA["Soma — mutable, sandboxed  (E2B MicroVMs)"]
            ORCH["Orchestrator\nLangGraph"] --> STRATEGY["Strategy"] & SELFMOD["Self-mod"] & ESCMGR["Escalation"]
        end

        SOMA -->|"dispatches"| NEURONS

        subgraph NEURONS["Neurons — specialised workers"]
            N1["content"] & N2["browser"] & N3["payments"] & N4["accounts"] & N5["analytics"]
        end

        NEURONS --> INFRA

        subgraph INFRA["Per-Darwin State"]
            WALLET["Treasury\nUSCD · x402"] & VAULT["Vault\nsecrets · KYC"] & ALOG["Audit log\nhash-chained"] & CKPT["Checkpoint\nDB"]
        end
    end

    NEURONS -->|"outbound via gateways"| EXT

    subgraph EXT["External World"]
        LLM["LLM\nLiteLLM proxy"] & BROW["Browser\nBrowserbase"] & PAY["Payments\nUSCD · Stripe"] & APIS["APIs\nads · blogs · …"]
    end

    subgraph ADAPTERS["darwin_platform / adapters"]
        A1["Web3ChainClient\nRealWallet"] & A2["HvacVaultClient\nRealVaultBackend"] & A3["BrowserUseClient"] & A4["X402PaymentClient"] & A5["E2BSandbox"] & A6["LiteLLMTransport"]
    end

    SUP -.->|"wires at spawn time"| ADAPTERS
    ADAPTERS -.-> WALLET & VAULT & BROW & PAY & LLM

    style PLATFORM fill:#1a1a2e,stroke:#4a4a8a,color:#e0e0ff
    style DARWIN  fill:#0d2137,stroke:#1a6b8a,color:#e0f0ff
    style DNA     fill:#0a1f0a,stroke:#2a6b2a,color:#d0ffd0
    style SOMA    fill:#1f1a0a,stroke:#7a6b1a,color:#fff0d0
    style NEURONS fill:#1f0a0a,stroke:#8a2a2a,color:#ffd0d0
    style INFRA   fill:#0a0a2f,stroke:#2a2a8a,color:#d0d0ff
    style EXT     fill:#1a1a1a,stroke:#555555,color:#dddddd
    style ADAPTERS fill:#1a0a1f,stroke:#6a2a8a,color:#f0d0ff
```

**Reading the diagram top to bottom:**

1. **Platform (blue)** — the boring multi-tenant SaaS layer. The Web app, Auth, Billing, and Notification services all funnel lifecycle commands through the **Darwin Supervisor**, which is the only component with the right to spawn, kill, pause, or resume a Darwin. It also enforces quotas and anomaly detection.

2. **Darwin Organism (teal)** — one isolated instance per user. Nothing leaks between organisms at the network or auth layer.
   - **DNA Core (green)** — the immutable, Ed25519-signed genome. Contains the constitution, the kill switch, the write-before-act audit logger, the treasury spend policy, and the cumulative layer stack (Layer 0 = founding genome, Layer N = lessons sealed after generation N). The organism can *read* it but never mutate it.
   - **Soma (amber)** — the mutable agent body, running inside E2B MicroVMs. The LangGraph orchestrator branches into three engines: *Strategy* (what to do next), *Self-modification* (proposes and tests soma rewrites), and *Escalation* (routes owner-approval requests back through the platform).
   - **Neurons (red)** — specialised worker sub-agents dispatched by the orchestrator. Each gets a scoped `NeuronContext` — only the capabilities it needs, nothing else.
   - **Per-Darwin State (purple)** — the organism's durable layer: USDC treasury with x402 programmable spend rules, a HashiCorp Vault namespace for secrets and KYC documents, the append-only hash-chained audit log, and the LangGraph checkpoint database for mid-task resumption.

3. **External World (grey)** — everything outside the organism boundary, reachable only outbound via the platform's gateways. LLM calls go through the LiteLLM proxy; browser sessions through Browserbase; payments over USDC/x402/Stripe.

4. **Adapters (violet, dashed)** — `darwin_platform/adapters` contains the real provider implementations. The Supervisor wires them into each Darwin at spawn time. When absent (tests, local sim), in-memory fakes drop in transparently via the same Protocol interface.

---

## Repository layout

| Path | Description |
| --- | --- |
| `src/darwin_dna/` | **Layer 0 genome** — signed, immutable Python package (≤ 5 k lines, curated-allowlist deps) |
| `  constitution/` | Immutable rule set encoded as pydantic models, signed at build time |
| `  evolution/` | Main loop, lesson distillation, reproduction event |
| `  neurons/` | Neuron contract + built-in specialised workers |
| `  machinery/` | Kill switch, audit logger, treasury policy |
| `  manifest.py` | SHA-256 + Ed25519 trust root |
| `  signature.py` | Layer signing and verification |
| `src/darwin_platform/` | **Platform control plane** — FastAPI supervisor |
| `  supervisor.py` | Spawn / monitor / kill Darwin instances, wires real adapters at spawn |
| `  api.py` | REST surface: `/darwins`, `/healthz`, `/readyz`, `/metrics` |
| `  leadership.py` | HA leader election via Postgres advisory locks — failover ≈ 5 s |
| `  persistence.py` | Dual-backend: SQLite file path or `postgres://` DSN |
| `  billing.py` | Per-Darwin USDC billing integrated with treasury policy |
| `  auth.py` | Bearer-token auth with `owner` and `platform_operator` scopes |
| `  escalation.py` | Owner-approval tickets for sensitive-tier vault reads |
| `  monitoring.py` | Prometheus metrics and alert counters |
| `  tracing.py` | OpenTelemetry / OTLP tracing (no-op shim when OTel is absent) |
| `  adapters/` | Real provider adapters: `Web3ChainClient`, `HvacVaultClient`, `BrowserUseClient`, `X402PaymentClient`, `E2BSandbox`, `LiteLLMTransport` |
| `src/darwin_fakes/` | **In-memory fakes** — deterministic, no network, no API keys required |
| `tests/` | Unit · property · integration · 90-day simulation suites |
| `tools/sim/` | Simulated world — runs a Darwin for N days and asserts invariants |
| `docker/` | Compose stack: supervisor + Postgres + LiteLLM + Prometheus + Jaeger |
| `deploy/helm/` | Kubernetes Helm chart (see [`deploy/helm/darwin-platform/README.md`](deploy/helm/darwin-platform/README.md)) |
| `docs/overview/` | Vision, design principles, prior art, market, legal, roadmap, infrastructure |
| `docs/engineering/` | Technical architecture and build plans (see [`docs/engineering/00-README.md`](docs/engineering/00-README.md)) |

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

## Documentation

Read [`docs/overview/`](docs/overview/) first for the idea; read [`docs/engineering/`](docs/engineering/) for implementation detail. Full indexes: [overview index](docs/overview/00-README.md) · [engineering index](docs/engineering/00-README.md).

### Overview — the idea

| Doc | Description |
| --- | --- |
| [01 — Vision and metaphor](docs/overview/01-vision-and-metaphor.md) | What Darwin is, the biological metaphor, why an organism rather than just an agent, the product experience, explicit non-goals |
| [02 — Design principles](docs/overview/02-design-principles.md) | P1–P10: immutable DNA, skin in the game, neurons as capability units, escalate-don't-fail, empirical fitness, auditability, kill-switches |
| [03 — Prior art](docs/overview/03-prior-art.md) | DGM, Voyager, agent frameworks, agentic payments, sandboxes — what's proven and what's new here |
| [04 — Market landscape](docs/overview/04-market-landscape.md) | Realistic revenue archetypes a Darwin can pursue |
| [05 — Legal and ToS](docs/overview/05-legal-and-tos.md) | Regulatory regimes, ToS checklist, liability surface |
| [06 — Roadmap and phases](docs/overview/06-roadmap-and-phases.md) | Four-phase build plan with definitions of done and kill criteria |
| [07 — Deployment and infrastructure](docs/overview/07-deployment-and-infrastructure.md) | Phase-by-phase infrastructure, Kubernetes shape, Terraform layout, DR |

### Engineering — the implementation

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
| [19 — System diagrams](docs/engineering/19-system-diagrams.md)                                   | ASCII reference renderings of the platform and the organism.                                                            |
| [20–25 — Build plans & ops](docs/engineering/00-README.md#build-plan--diagrams)                 | Phase-zero through phase-two build plans, data model reference, threat model, operational runbook.                      |

---

## Tech stack

Key technology choices at a glance. Full rationale in [`docs/engineering/11-tech-stack-at-a-glance.md`](docs/engineering/11-tech-stack-at-a-glance.md).

| Concern | Choice | Why |
| --- | --- | --- |
| Language | Python 3.12 | Largest agent/AI ecosystem; LangGraph, Browser-Use, E2B SDKs are all Python-native |
| Orchestration | LangGraph | Stateful, durable execution, HITL primitives, model-agnostic, most-deployed for stateful agents in 2026 |
| LLM gateway | LiteLLM Proxy (self-hosted) | 100+ providers behind one OpenAI-compatible interface, per-request cost attribution, virtual keys per Darwin |
| Sandboxing | E2B (Firecracker MicroVMs) | Strongest isolation, purpose-built for untrusted agent code, fast cold starts |
| Browser | Browser-Use + Browserbase | Python-native, autonomous DOM exploration, anti-bot infra and session recording handled by Browserbase |
| Database | Postgres 16 + pgvector | RLS for tenant isolation, pgvector for embeddings, no extra vector DB needed |
| Vault | HashiCorp Vault | Namespace per Darwin, agent-aware identity model, dynamic secrets, multi-tenant battle-tested |
| Wallet | Coinbase Agentic Wallets | TEE-secured keys, native x402 support, programmable spend limits, gasless on Base |
| Payments | x402 (USDC on Base) | HTTP-native, machine-to-machine, >50 M txns in production by early 2026 |
| Containers | Kubernetes (EKS / GKE) | Required for E2B at scale, ample multi-tenant patterns |
| Observability | OpenTelemetry + Honeycomb | Vendor-neutral instrumentation, high-cardinality querying |

---

## Testing strategy

Tests live in `tests/` and are split into three tiers:

| Tier | Marker | Run command | What it covers |
| --- | --- | --- | --- |
| **Unit + property** | *(default)* | `uv run pytest -q` | Individual subsystems in isolation. The four hot paths (`signature`, `manifest`, `kill_switch`, `audit_logger`, `treasury_policy`) are tested with a malicious soma assumed — adversarial inputs, tampered layers, over-limit spends. Hypothesis generates thousands of random cases for each property. |
| **Integration** | *(default)* | `uv run pytest -q` | Full boot cycle with `FakeStack`. Verifies that the orchestrator, neurons, wallet, vault, and platform API compose correctly end-to-end without any real network calls. |
| **Simulation** | `slow` | `uv run pytest -m slow -q` | 90 simulated days. A Darwin is spawned, evolves, and optionally reproduces while the test suite asserts financial invariants, audit-chain integrity, and kill-switch latency under load. Takes several minutes; marked `slow` so it's excluded from the fast commit loop. |

Run the full suite:

```bash
uv run pytest -q          # fast loop (unit + integration, ~60 s)
uv run pytest -m slow -q  # includes simulation (~5 min)
```

---

## Roadmap

What's planned beyond the current Layer 0 implementation. See [Roadmap and phases](docs/overview/06-roadmap-and-phases.md) for the phased plan and [`docs/engineering/`](docs/engineering/) for technical design detail behind each item.

**Near-term (platform hardening)**
- [ ] `envFrom` as a first-class Helm chart value for cleaner secret injection
- [ ] Webhook ingestion for Stripe and Coinbase events
- [ ] Harvest scheduler: automatic profit extraction on configurable rules
- [ ] OIDC layer at the Ingress for human-facing auth (Clerk / WorkOS)

**Medium-term (organism capabilities)**
- [ ] LanceDB per-Darwin vector store for long-horizon episodic memory
- [ ] Stagehand cached-replay integration for repetitive browser flows
- [ ] Ads and trading neurons beyond the current content + browser + payments set
- [ ] Temporal Cloud for durable billing, harvest, and reproduction job orchestration

**Long-term (evolutionary system)**
- [ ] Fitness-weighted automated reproduction triggering (no `force` required)
- [ ] Cross-lineage lesson distillation — sealed lessons surfaced as platform-level priors
- [ ] On-chain Merkle anchoring for the audit log (daily root, tamper-evident)
- [ ] Kubernetes CRD / operator for Darwin lifecycle (`kind: Darwin`)

---

## Security

**Responsible disclosure:** if you discover a vulnerability — especially in the kill switch, audit logger, treasury policy, or any code that handles cryptographic signatures or private keys — please report it privately before opening a public issue.

Contact: **security@darwinagents.com** (PGP key available on request).

**Scope of highest concern:**
- Bypass of the kill switch or pause mechanism
- Forgery or replay of a signed DNA layer
- Unauthorized spend exceeding treasury policy hard caps
- Secret exfiltration from a Darwin's Vault namespace to another tenant

**Out of scope:** UI bugs, documentation errors, issues with optional extras not installed in production.

We aim to acknowledge reports within 48 hours and provide a fix timeline within 7 days for critical issues.

---

## Contributing

This is a private repository. If you have access, please follow these conventions:

**Branching**
- `main` is always deployable. Feature work goes on `feat/*`, fixes on `fix/*`.
- Open a PR against `main`; at least one review is required before merge.

**Commit style** — [Conventional Commits](https://www.conventionalcommits.org/):
```
feat(neurons): add analytics.reader neuron
fix(treasury): clamp daily spend before soft-cap comparison
docs(helm): document envFrom injection pattern
```

**Code style**
```bash
uv run ruff check .    # lint (F, E9, PLE rule set)
uv run ruff format .   # format
```
Ruff is enforced in CI. The genome's dependency allowlist (`pyproject.toml [project.dependencies]`) is intentionally minimal — new deps in `darwin_dna` require explicit justification.

**Tests** — new behaviour must ship with tests. Hot-path changes (`kill_switch`, `treasury_policy`, `signature`, `audit_logger`, `manifest`) require property-based tests (Hypothesis) in addition to unit tests.

---

## License

Copyright (c) 2026 [Specimen Labs, Inc.](https://specimenlabs.com) — All rights reserved. See [LICENSE](LICENSE) for the full terms.

---

## Boot guarantee

Layer 0 boots a Darwin into a _working_ state on first run — naive and unprofitable is acceptable, non-functional is not. The loop runs, neurons dispatch, the ledger reconciles, the kill switch halts, the audit chain is unbroken. Evolution improves everything above that floor; nothing falls below it.
