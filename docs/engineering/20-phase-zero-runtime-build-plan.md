# Phase 0 — runtime integration build plan

Doc 18 specified Layer 0 (the founding genome) in isolation. This companion doc specifies everything *around* it: the supervisor spawn sequence, the adapter wiring, persistence, checkpoint recovery, observability, Docker Compose, and the soak-test harness. Together, docs 18 + 20 constitute the full Phase 0 deliverable: **one Darwin, running end-to-end, on real infrastructure, for at least 24 hours straight**.

## What Phase 0 proves

Phase 0 is a plumbing exercise. We are not proving the agent is smart, profitable, or self-improving. We are proving:

1. The genome boots, loops, and reconciles its ledger against a wallet.
2. The platform spawns, monitors, pauses, kills, and persists the organism across process restarts.
3. Neurons dispatch against real (or realistically-faked) substrates.
4. Every action is hash-chained in the audit log and survives restarts.
5. A kill-switch flip halts the Darwin in < 5 seconds.
6. The infrastructure cost of keeping one Darwin alive is within budget (< $20/day LLM + infra).

## The spawn sequence

When `POST /darwins` arrives at the supervisor:

```
  API layer (api.py)
    │
    ▼
  Supervisor.spawn(opening_usdc, ...)
    │
    ├─ 1. Generate darwin_id (uuid4)
    ├─ 2. Mint identity + manifest (signature.py, manifest.py)
    ├─ 3. Build FakeStack or real-adapter Platform bundle
    │       ├── wallet (FakeWallet | RealWallet+Web3ChainClient)
    │       ├── vault  (FakeVault | RealVaultBackend+HvacVaultClient)
    │       ├── browser (FakeBrowser | BrowserUseClient on Browserbase)
    │       ├── sandbox (FakeSandbox | E2BSandbox)
    │       ├── x402 (FakeX402 | X402PaymentClient)
    │       ├── llm transport (FakeLLM | LiteLLMTransport)
    │       ├── escalation inbox
    │       ├── signer (platform signing service — shared per Supervisor)
    │       └── spawner (child-creation port for reproduction)
    │
    ├─ 4. Call darwin_dna.evolution.boot(platform_bundle)
    │       ├── verify manifest signature (trust root)
    │       ├── assert constitution invariants
    │       ├── build orchestrator graph (LangGraph)
    │       ├── wire machinery (kill switch, audit logger, ledger, treasury policy)
    │       └── return DarwinRuntime
    │
    ├─ 5. Persist lifecycle record (persistence.py)
    ├─ 6. Create asyncio task → _drive_loop(handle)
    │       └── loop: heartbeat → run(max_heartbeats=1) → observe children → billing sweep
    │
    └─ 7. Return DarwinStatus to caller
```

The supervisor never reaches into the runtime's state. All lifecycle transitions are flag-flips that the genome polls at node boundaries.

## Module map (what already exists)

```
src/
  darwin_platform/
    __main__.py              CLI: `darwin-platform serve` — wires args → Supervisor → FastAPI
    api.py                   HTTP surface: doc-08 endpoints, auth middleware, request models
    supervisor.py            Lifecycle owner: spawn, drive loop, kill, pause, lineage, billing
    persistence.py           SQLite / Postgres write-through for audit, billing, lineage, lifecycle
    leadership.py            HA: Postgres advisory lock election, leader/follower transitions
    monitoring.py            Anomaly detection: spend velocity, failure rate, heartbeat, escalation
    escalation.py            Platform-side ticket routing + notification sink
    billing.py               Per-Darwin USDC metered billing (markup + harvest fee)
    auth.py                  Bearer-token auth: owner / platform_operator scopes
    tracing.py               OpenTelemetry spans (NoOp when SDK absent)
    provisioning.py          Platform-mediated resource provisioning (DB, bucket, jobs)
    harvest_scheduler.py     Recurring profit-harvest rules
    sanctions.py             OFAC / denylist screening on outbound wallet transfers
    throttle.py              Non-LLM resource throttles (browser, sandbox, egress)
    browser_cache.py         Workflow replay cache (Browser-Use → Stagehand promotion)
    vault_admin.py           Owner-write vault admin (bidirectional shared vault)
    watermark.py             HMAC watermarking for vault entries (forensic provenance)
    snapshot.py              Point-in-time cluster snapshot (debugging, support)
    operator_audit.py        Hash-chained operator audit log
    adapters/
      wallet.py              RealWallet wrapping Web3ChainClient
      web3_chain.py          EVM chain client (Base Sepolia / mainnet USDC)
      hvac_vault.py          HashiCorp Vault hvac client
      vault_backend.py       RealVaultBackend wrapping hvac
      browser.py             BrowserUseClient on Browserbase
      sandbox.py             E2BSandbox (code execution microVM)
      x402.py                x402 payment protocol client (httpx)
      litellm_transport.py   Real LLM transport via LiteLLM proxy

  darwin_fakes/
    __init__.py              make_platform() — in-memory deterministic stand-ins for all ports
    fake_platform.py         FakeStack, SigningService, spawner, checkpoint
    fake_llm.py              Deterministic LLM responses (no API keys, reproducible)
    fake_wallet.py           In-memory USDC ledger (testnet-shaped)
    fake_vault.py            In-memory secret store
    fake_sandbox.py          Stub code execution

  tools/sim/
    run_sim.py               90-day invariant simulation (8 invariants, doc 18 nightly tier)
    world.py                 Simulated external world (revenue events, cost draws)
    scenario.py              Named scenarios (default, hostile, drought, etc.)
    calibration.py           Hyperparameter sensitivity sweeps
```

## Adapter wiring matrix

The supervisor supports per-slot real/fake selection. When a CLI flag (or its env-var fallback) is present, that adapter slot switches to the real provider; otherwise the in-memory fake is used. This lets us run anywhere on the spectrum from "fully deterministic" (tests, 90-day sim) to "fully real" (dev EKS against testnet).

| Adapter slot        | Fake (default)            | Real (flag-gated)                     | Flag / env                                      |
| ------------------- | ------------------------- | ------------------------------------- | ----------------------------------------------- |
| **Wallet**          | `FakeWallet`              | `RealWallet` + `Web3ChainClient`      | `--rpc-url` + `--wallet-key`                    |
| **Vault**           | `FakeVault`               | `RealVaultBackend` + `HvacVaultClient` | `--vault-url` + `--vault-token`                |
| **Browser**         | `FakeBrowser`             | `BrowserUseClient` (Browserbase)      | `--browserbase-api-key`                         |
| **Sandbox**         | `FakeSandbox`             | `E2BSandbox`                          | `--e2b-api-key`                                 |
| **x402**            | Fake (stubbed in stack)   | `X402PaymentClient` (httpx)           | Auto when wallet is real                        |
| **LLM**            | `FakeLLM` (deterministic) | `LiteLLMTransport` (proxy at :4000)   | `--litellm-url` (future) / compose service      |
| **Persistence**     | In-memory                 | SQLite file / Postgres DSN            | `--db <path-or-dsn>`                            |
| **Auth**            | Trust-all                 | Token-scoped (owner / operator)       | `--auth-tokens <json>`                          |
| **Sanctions**       | None (pass-through)       | `LocalDenylistScreener`               | `--sanctions-denylist <json>`                   |

## Docker Compose (local dev environment)

The existing `docker/` directory provides a one-command stack:

```
docker compose up --build
```

Services:

| Service       | Port  | Purpose                                                      |
| ------------- | ----- | ------------------------------------------------------------ |
| `supervisor`  | 8000  | FastAPI control plane + drive loop (SQLite at `/data/state.db`) |
| `postgres`    | 5432  | Postgres backend (when `--db` points here)                   |
| `litellm`     | 4000  | BerriAI LiteLLM proxy (real model keys from `.env`)          |
| `prometheus`  | 9090  | Scrapes supervisor `/metrics` every 15s                      |
| `jaeger`      | 16686 | OpenTelemetry trace UI (receives on 4317/4318)               |

The supervisor container defaults to SQLite persistence. To switch to Postgres, override the CMD in `docker-compose.yml`:

```yaml
command: ["python", "-m", "darwin_platform", "serve",
          "--host", "0.0.0.0", "--port", "8000",
          "--db", "postgresql://darwin:pass@postgres:5432/darwin",
          "--leader-election"]
```

## Persistence and checkpoint recovery

The persistence layer (doc-08, `persistence.py`) provides restart-survivable storage for:

| Collection       | What persists                                                 |
| ---------------- | ------------------------------------------------------------- |
| `audit_entries`  | Hash-chained operator audit log (never deleted, even on death) |
| `billing_ledger` | Per-Darwin USDC metered charges + harvest fees                |
| `escalation_tickets` | Routed tickets + status transitions                       |
| `alert_history`  | Anomaly monitor alerts with cooldown                          |
| `lineage_edges`  | Parent→child edges (queryable after Darwin death)             |
| `darwin_lifecycle` | Per-Darwin: alive, killed, spawned_at, balance snapshot     |
| `manifests`      | Signed manifest per darwin_id (re-boots verify same identity) |
| `checkpoints`    | LangGraph state checkpoints (resume mid-graph on restart)     |
| `auth_tokens`    | Issued bearer tokens + scopes                                 |
| `platform_signing_key` | HSM stand-in — persisted Ed25519 private key bytes     |

**Recovery on restart:** the supervisor reloads `darwin_lifecycle` rows where `alive=True`, re-mints the `Platform` bundle with the persisted manifest and signing key, re-`boot`s each Darwin, and resumes the drive loop from the last saved checkpoint. The genome's audit chain remains unbroken because it was persisted before the crash.

## HA leader election

When deployed with `--leader-election` against a shared Postgres:

- Exactly one replica holds a `pg_advisory_lock` and runs all drive loops.
- Followers serve `/healthz` but report `503` on `/readyz` so the k8s Service routes only to the leader.
- On leader loss (pod eviction, connection drop), the lock auto-releases; the first follower to poll wins and takes over.
- The transition is transparent to the genomes — they just see a pause in heartbeats, not a state loss.

## Observability

| Layer            | Tool             | What it captures                                              |
| ---------------- | ---------------- | ------------------------------------------------------------- |
| **Traces**       | OpenTelemetry    | Span per: spawn, heartbeat, neuron dispatch, payment, escalation, mutation |
| **Metrics**      | Prometheus       | `darwin_heartbeat_total`, `darwin_spend_usdc`, `darwin_neuron_dispatch_total`, custom gauges |
| **Alerts**       | Monitor + sink   | Spend velocity, failure rate, heartbeat health, escalation rate → graded response |
| **Logs**         | structlog (JSON) | Every supervisor action + genome audit events (forwarded)      |
| **Dashboards**   | Jaeger UI        | Trace explorer for latency/error debugging                     |

The tracing module (`tracing.py`) is NoOp when the OTEL SDK is absent. In Compose, the Jaeger service receives traces on OTLP gRPC (4317) and HTTP (4318).

## The soak test — proving Phase 0

The soak test is Phase 0's acceptance gate. It runs a real Darwin against real (or compose-level) infrastructure for 24+ hours and asserts the roadmap's definition of done.

### Pass criteria (from `docs/overview/06-roadmap-and-phases.md`)

| # | Criterion                                                                      | How we measure                                         |
| - | ------------------------------------------------------------------------------ | ------------------------------------------------------ |
| 1 | Darwin spawns and runs for 24h straight without crashing                       | Process uptime; no unhandled exceptions in the log     |
| 2 | ≥ 50 neuron dispatches with ≥ 80% success rate                                | `DarwinStatus.tick`, neuron dispatch counters           |
| 3 | Every action appears in the event log with a valid hash chain                  | `audit_log.verify()` returns True at end of run        |
| 4 | Ledger reconciles against (mock) wallet balance with zero discrepancy          | `ledger.balance == wallet.balance` assertion           |
| 5 | Kill-switch flip halts the Darwin in < 5 seconds                               | Timed: `POST /kill` → `DarwinStatus.alive == False`   |
| 6 | We can replay any moment of the Darwin's life from the event log               | Reconstruct state from persisted events; diff = empty  |

### Implementation plan

```
tools/soak/
  run_soak.py              Entry point: spawns via HTTP, monitors, asserts criteria
  assertions.py            Per-criterion assertion functions (reusable in CI)
  config.py                Duration, thresholds, endpoint URL, polling interval
```

The soak test:
1. Starts a fresh supervisor (compose or bare metal with `--db`).
2. `POST /darwins` with a configured `opening_usdc`.
3. Polls `GET /darwins/{id}` every 60s, logs progress.
4. After the configured duration, runs all 6 assertions.
5. Sends a kill and measures latency (criterion 5).
6. Fetches the full event log, verifies the hash chain, replays the ledger (criteria 3, 4, 6).
7. Exits 0 on pass, non-zero on any failure — CI-friendly.

### Running the soak

```bash
# Against docker compose (default):
docker compose up -d
uv run python -m tools.soak.run_soak --duration 24h --url http://localhost:8000

# Against dev EKS:
uv run python -m tools.soak.run_soak --duration 24h --url https://darwin-dev.internal:8000 \
  --auth-token owner-dev-token
```

## What remains to build (vs. what's already shipped)

| Component                       | Status     | Notes                                                       |
| ------------------------------- | ---------- | ----------------------------------------------------------- |
| Supervisor spawn + drive loop   | **Done**   | `supervisor.py` — asyncio task per Darwin                   |
| Doc-08 HTTP API                 | **Done**   | `api.py` — full endpoint surface with auth                  |
| Persistence (SQLite + Postgres) | **Done**   | `persistence.py` — dual-backend, all collections            |
| HA leader election              | **Done**   | `leadership.py` — Postgres advisory locks                   |
| Real adapters (wallet, vault, browser, sandbox, x402) | **Done** | `adapters/` — flag-gated wiring in `__main__.py` |
| Docker Compose stack            | **Done**   | `docker/` — supervisor, litellm, prometheus, jaeger         |
| Anomaly monitor + graded response | **Done** | `monitoring.py` + `throttle.py`                             |
| Platform billing                | **Done**   | `billing.py` + persistence integration                      |
| Sanctions screening             | **Done**   | `sanctions.py` — LocalDenylistScreener                      |
| Browser workflow cache           | **Done**   | `browser_cache.py` — promotion from Browser-Use to replay   |
| Provisioning service            | **Done**   | `provisioning.py` — quota-enforced, audit-logged            |
| Vault admin (bidirectional)     | **Done**   | `vault_admin.py` + `watermark.py`                           |
| Harvest scheduler               | **Done**   | `harvest_scheduler.py` — recurring profit extraction        |
| 90-day invariant simulation     | **Done**   | `tools/sim/` — 8 invariants, multiple scenarios             |
| OpenTelemetry tracing           | **Done**   | `tracing.py` — NoOp shim when SDK absent                   |
| Soak test harness (24h)         | **TODO**   | `tools/soak/` — the Phase 0 acceptance gate                 |
| Next.js dashboard (minimal)     | **TODO**   | List Darwins + event log view (Phase 0 MVP)                 |
| Helm chart dev values           | **Done**   | `infra/charts/` — values.yaml + templates                    |
| Dev EKS Terraform               | **TODO**   | AWS EKS + Aurora + ElastiCache IaC                          |

## The minimal dashboard (Phase 0 scope)

Phase 0 does not require a polished UI. It requires proof that a human operator can observe the organism from outside. The minimum:

1. **Darwin list** — table of all spawned Darwins with status (alive/dead/paused), balance, tick count, age.
2. **Event log** — paginated, filterable event stream for one Darwin (the hash-chained audit trail).
3. **Kill button** — one-click kill with confirmation.

This can be a single Next.js page hitting the existing `GET /darwins`, `GET /darwins/{id}/events`, and `POST /darwins/{id}/kill` endpoints. Auth via the same bearer token the API already supports.

## Definition of done (Phase 0)

Phase 0 is declared complete when:

- [ ] The soak test passes against Docker Compose with the FakeLLM (proving the loop + infrastructure).
- [ ] The soak test passes with a real LiteLLM proxy (proving real LLM costs are within budget).
- [ ] The ledger reconciles against the FakeWallet (and optionally a testnet wallet) with zero discrepancy.
- [ ] A process restart mid-soak (kill -9 the supervisor, restart) results in the Darwin resuming from checkpoint with zero lost audit entries.
- [ ] The Helm chart deploys to a dev EKS cluster and a Darwin survives a node eviction (leader failover).
- [ ] ≥ 1 engineer who didn't write the supervisor can spawn, observe, and kill a Darwin using only the HTTP API and the event log (operability check).

After Phase 0 is met, the next build plan is **Phase 1 — the evolution loop** (self-modification staging, reproduction, real economic activity). That doc will be [`21-phase-one-evolution-build-plan.md`](21-phase-one-evolution-build-plan.md).
