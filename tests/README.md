# Darwin tests

One-page map of the test suite. The long-form rationale — what each tier
proves, how it maps to the threat model, and the CI split — lives in
[`docs/engineering/26-testing-strategy.md`](../docs/engineering/26-testing-strategy.md);
this file tells you where things go and how to run them.

## Layout

```
tests/
├── conftest.py                 # shared fixtures: vclock, signer, genesis, booted
├── README.md                   # this file
│
├── unit/                       # tier 1 — one module per test, fakes only (mirrors src/)
│   ├── darwin_dna/
│   │   ├── constitution/       # spend limits, forbidden actions, escalation rules
│   │   ├── machinery/          # kill switch, audit, ledger, treasury, vault,
│   │   │                       #   patch validator, fitness, memory, eval harness, …
│   │   ├── evolution/          # reproduction, lineage, compaction, amendments
│   │   ├── neurons/            # per-neuron contracts
│   │   ├── orchestrator_skeleton/
│   │   └── test_{signature,identity,hyperparameters,untrusted}.py
│   └── darwin_platform/        # one folder per bounded context
│       ├── runtime/            # supervisor, persistence (SQLite/Postgres/RLS), leadership, audit sink
│       ├── tenancy/            # provisioning, Vault admin, namespaces, cross-pollination
│       ├── payments/           # billing, omnibus, ramps, harvest, sanctions
│       ├── security/           # auth, Clerk, KYC, signing, key rotation, watermark
│       ├── observability/      # monitoring, drift, Merkle anchor, operator audit, sinks
│       ├── escalations/        # escalation router, Web Push (+ legacy FCM sink)
│       ├── evolution/          # patch staging, eval runner, soma loader / merge
│       ├── infra/              # egress proxy, throttles, browser cache
│       ├── adapters/           # wallet/chain, vault, browser, fiat, LiteLLM, x402, …
│       ├── k8s/                # driver, manifests, namespace backend
│       └── client/             # typed Python SDK
│
├── property/                   # tier 2 — Hypothesis invariants (kill switch, trust root)
├── redteam/                    # tier 3 — one file per doc-24 threat class (9 files)
├── integration/                # tier 4 — multi-module flows in-process (HTTP via ASGI)
├── infra/                      # tier 5 — Docker, Compose, Helm, Terraform, CI, k8s policy,
│                               #   observability, and config-reference contracts
├── e2e/                        # tier 6 — the real `darwin-platform serve` process
│   └── conftest.py             #   Server harness, server_factory, tokens, wait_for
├── performance/                # tier 7 — latency / throughput / memory budgets
│   └── conftest.py             #   perf fixture (best_of, p95, peak_bytes, budget)
└── simulation/                 # tier 8 — multi-day in-process sims
```

## Tiers and markers

Every tier's `conftest.py` applies its marker automatically — never decorate
tests by hand. The e2e, performance, and simulation tiers also add `slow`,
so the fast loop skips them.

| Tier | Marker(s) | Make target | Runs in CI |
| ---- | --------- | ----------- | ---------- |
| `unit/` | `unit` | `make test-unit` | every push |
| `property/` | `property` | `make test-property` | every push |
| `redteam/` | `redteam` | `make test-redteam` | every push + release gate |
| `integration/` | `integration` | `make test-integration` | every push |
| `infra/` | `infra` | `make test-infra` | every push |
| `e2e/` | `e2e`, `slow` | `make test-e2e` | every PR (own job) + main |
| `performance/` | `performance`, `slow` | `make test-perf` | main + weekly |
| `simulation/` | `simulation`, `slow` | `make test-simulation` | main + weekly |

## Running

From the repo root; [`Makefile`](../Makefile) targets mirror these (`make help`).

```bash
uv run pytest -m "not slow"     # fast loop — what every push runs (make test-fast)
uv run pytest -m e2e            # the real server over HTTP/WS     (make test-e2e)
uv run pytest -m slow           # sim + e2e + performance          (make test-slow)
uv run pytest                   # everything                       (make test-all)

uv run pytest tests/infra -rs   # -rs lists the helm/terraform/docker checks that skipped
DARWIN_PERF_SCALE=3 uv run pytest tests/performance   # 3x budgets on a slow machine

# Postgres-backed persistence / RLS / leader election (needs Postgres on :5432)
make test-postgres
```

## Where a new test goes

| You are testing… | Put it in |
| ---------------- | --------- |
| one module of `src/` | `unit/<package>/<area>/test_<module>.py` |
| an invariant over generated inputs | `property/test_<property>_properties.py` |
| an attack from doc 24 | `redteam/test_<threat_class>.py` (one file per threat) |
| a flow across modules, in-process | `integration/test_<flow>.py` |
| a deployment asset or the config docs | `infra/test_<asset>.py` |
| behaviour only the real process shows (CLI, lifespan, restart, sockets) | `e2e/test_<journey>.py` |
| a latency / memory budget or a scaling property | `performance/test_<area>_perf.py` |
| a multi-day invariant | `simulation/test_<scenario>.py` |

Use the fakes in `darwin_fakes`, not mocks; assert the safety property and
that the defence was *visible* (audit record, alert, status). See doc 26's
checklist.
