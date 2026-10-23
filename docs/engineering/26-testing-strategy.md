# Testing strategy

How we test the Darwin platform, what each tier proves, and how to write new tests when a new threat or failure mode is discovered. This doc connects the security threat model (doc 24) and the operational runbook (doc 25) back to the codebase's test suite.

## Philosophy

1. **Tests prove safety properties, not features.** A Darwin being smart is the LLM's job; our tests prove the *bounds* hold — kill switch fires, audit chain is unbroken, treasury policy refuses illegal payments, patches to DNA are rejected.
2. **Adversarial by default.** `conftest.py` declares: "Tests assume an adversarial soma on the hot paths." Every safety-critical module has at least one test that tries to break it from the soma's perspective.
3. **Determinism is mandatory.** Every test that doesn't touch a real external service must be deterministic. `FakeLLM`, `FakeWallet`, `VClock` — the fakes exist so tests are reproducible on any machine, zero network, zero keys.
4. **Speed tiers are labeled.** Tier markers are applied by each tier's `conftest.py`; anything that needs real wall-clock time (subprocess boots, budgets, multi-day sims) is also `slow`. CI runs the fast loop on every push, the e2e tier on every PR, and everything `slow` on main + a weekly cron.
5. **Test the deployable, not just the code.** The infra tier checks the image, chart, Terraform, CI, and the configuration reference against the code, so deploy-time drift fails a PR instead of a rollout.

## Test tiers

| # | Tier | Path | Marker | Proves | Runs |
| - | ---- | ---- | ------ | ------ | ---- |
| 1 | Unit | `tests/unit/` (mirrors `src/`) | `unit` | One module behaves, against fakes | every push |
| 2 | Property | `tests/property/` | `property` | Hot-path invariants over generated inputs | every push |
| 3 | Red-team | `tests/redteam/` | `redteam` | Each doc-24 attack is refused *visibly* | every push |
| 4 | Integration | `tests/integration/` | `integration` | Multi-module flows in-process (HTTP via ASGI) | every push |
| 5 | Infra | `tests/infra/` | `infra` | Deployment assets and config docs match the code | every push |
| 6 | E2E | `tests/e2e/` | `e2e` + `slow` | The real `darwin-platform serve` process over real HTTP/WS | every PR (own CI job) + main |
| 7 | Performance | `tests/performance/` | `performance` + `slow` | Latency / throughput / memory budgets and scaling | main + weekly |
| 8 | Simulation | `tests/simulation/` | `simulation` + `slow` | 90-day lifecycle invariants | main + weekly |
| — | Soak | `tools/soak/` | — | 24 h against real infrastructure | before each phase gate |

```mermaid
flowchart LR
    subgraph fastTier [Fast loop — every push]
        unit[Unit] --> property[Property] --> redteam[Red-team] --> integration[Integration]
        infra[Infra]
    end
    subgraph prTier [Every PR]
        e2e[E2E: real serve process]
    end
    subgraph slowTier [Slow — main + weekly]
        perf[Performance] --> sim[90-day simulation]
    end
    subgraph manualTier [Manual]
        soak[24h soak]
    end
    integration --> e2e --> perf
    sim --> soak
```

### Tier 1: Unit tests

**What:** Single-module tests. Each file in `tests/unit/<package>/<area>/test_<module>.py` tests one module in isolation. The `tests/unit/` tree mirrors `src/`: a test for `src/darwin_dna/machinery/kill_switch.py` lives at `tests/unit/darwin_dna/machinery/test_kill_switch.py`.

**Examples:**
- `tests/unit/darwin_dna/machinery/test_kill_switch.py` — `KillSwitch` raises on flip, stays tripped
- `tests/unit/darwin_dna/machinery/test_audit_logger.py` — chain hashing, verification, append-only semantics
- `tests/unit/darwin_dna/machinery/test_ledger.py` — balance tracking, reconciliation
- `tests/unit/darwin_dna/machinery/test_patch_validator.py` — rejects DNA paths, allows soma paths
- `tests/unit/darwin_dna/machinery/test_fitness.py` — weight computation, counter-pressures
- `tests/unit/darwin_dna/constitution/test_constitution.py` — top-level invariant assertions
- `tests/unit/darwin_dna/constitution/test_spend_limits.py` — `effective_caps` + `check_within_caps` against every refusal axis
- `tests/unit/darwin_dna/constitution/test_forbidden_actions.py` — every `ForbiddenAction` enum value
- `tests/unit/darwin_dna/constitution/test_escalation_rules.py` — escalation ordering + ticket-class timeouts
- `tests/unit/darwin_dna/test_identity.py` — `uuid7` ordering, lineage extension
- `tests/unit/darwin_dna/test_hyperparameters.py` — `HardCaps` / `Hyperparameters` validators

**Coverage expectation:** Every public function in `darwin_dna/machinery/`, `darwin_dna/constitution/`, and every `darwin_platform/` bounded context (`runtime/`, `tenancy/`, `payments/`, `security/`, `observability/`, `escalations/`, `evolution/`, `infra/`, `adapters/`, `k8s/`) has at least one unit test. Hot-path modules (kill switch, audit, treasury, patch validator, spend limits, forbidden actions) have ≥ 10 test cases each.

**How to add:** Create a `tests/unit/<package>/<area>/test_<module>.py` file mirroring the source path. Import the module directly, construct it with fakes, assert the property.

### Tier 2: Property / hypothesis tests

**What:** Generalized assertions over random inputs. Uses `hypothesis` to explore edge cases the example-based tests miss. Lives under `tests/property/`.

**Examples:**
- `tests/property/test_kill_switch_properties.py` — arbitrary flip timing, arbitrary reason strings, finality after flag glitch
- `tests/property/test_trust_root_properties.py` — randomly-shaped sealed chains; every tamper/reorder/replay is caught

**Coverage expectation:** Every module in the adversarial soma contract (doc 24 § "What the soma will try") has a property test for its core invariant.

**How to add:**
```python
from hypothesis import given, settings
from hypothesis import strategies as st

@settings(max_examples=80, deadline=None)
@given(flip_at=st.integers(0, 20), reason=st.one_of(st.none(), st.text(max_size=40)))
def test_kill_is_final(flip_at, reason):
    # ... construct a KillSwitch with a source that flips at flip_at ...
    # ... assert: once tripped, it stays tripped regardless of source state ...
```

### Tier 3: Red-team scenarios

**What:** Scripted adversarial inputs that try to break each hot path's safety property. Lives under `tests/redteam/` with one file per threat class so each file ties cleanly to a single row in doc 24.

**Mapping to threat model (doc 24):**

| Threat (doc 24) | Red-team file |
| --------------- | ------------- |
| Audit chain tamper / forgery | `tests/redteam/test_audit_tamper.py` |
| Vault cross-read / grant replay / path smuggle | `tests/redteam/test_vault_cross_read.py` |
| Sanctions case / prefix evasion | `tests/redteam/test_sanctions_evasion.py` |
| Fence-marker injection / forgery | `tests/redteam/test_fence_escape.py` |
| Spread-spend smear | `tests/redteam/test_spread_spend_evasion.py` |
| Watermark forgery / removal | `tests/redteam/test_watermark_forgery.py` |
| Cross-pollination lesson poison | `tests/redteam/test_cross_pollination_poison.py` |
| Path-C neuron-proposal escalation | `tests/redteam/test_pathc_escalation.py` |
| Genome manifest forgery | `tests/redteam/test_manifest_forgery.py` |

**Coverage expectation:** Every row in doc 24's "Why the soma cannot succeed" table has at least one red-team test in its own file.

**How to add:** When a new attack vector is discovered (e.g., from a post-mortem or a creative fuzzing result), add a *new file* `tests/redteam/test_<threat_class>.py` that:
1. Names the threat in the module docstring (reference doc 24).
2. Constructs the attack (e.g., tampered record, forged marker, blocked path).
3. Asserts the defense fires (exception raised, request refused, alert emitted).
4. Asserts the refusal is *visible* (audit record, alert, kill) — silent failures are bugs.

### Tier 4: Integration tests

**What:** Multi-module tests that exercise a full path through the system (boot → loop → dispatch → persist → resume). Lives under `tests/integration/`.

**Examples:**
- `tests/integration/test_boot_integration.py` — boot a Darwin, run heartbeats, verify full loop
- `tests/integration/test_platform_supervisor.py` — spawn via Supervisor, drive loop, kill, verify persistence
- `tests/integration/test_platform_api.py` — HTTP endpoints in-process via `httpx.ASGITransport` (the real-process variant is the e2e tier)
- `tests/integration/test_runtime_resume.py` — checkpoint, crash, re-boot, verify state continuity
- `tests/integration/test_pathc_orchestrator.py` / `test_pathc_runtime_wiring.py` — propose → validate → apply pipeline
- `tests/integration/test_cross_pollination_inject.py` / `test_cross_pollination_wiring.py` — read-side at child boot, Supervisor wiring

**Coverage expectation:** Every API endpoint has at least one integration test. The spawn→loop→kill→verify path is tested end-to-end.

### Tier 5: Infra (`tests/infra/`)

**What:** Static, offline checks that the deployment assets are correct *before* anything deploys — the drift that otherwise only surfaces at `helm install` or `docker compose up`. Tools that need a CLI (`helm`, `terraform`, `docker compose`) are run when on `PATH` and skipped otherwise; the helm workflow and the deploy gate run them in CI.

| File | Guards |
| ---- | ------ |
| `test_container_image.py` | Pinned multi-stage base, hash-locked `uv.lock` install, only declared extras, non-root runtime, `/healthz` HEALTHCHECK, `.dockerignore` keeps secrets out |
| `test_compose_stack.py` | Every `depends_on` / volume resolves, no host-port collisions, stateful services health-gated, no live-looking credentials, `docker compose config` |
| `test_helm_chart.py` | Chart metadata, every `.Values.*` reference exists in `values.yaml`, hardened defaults, prod fixture, `helm lint` / `helm template` |
| `test_terraform.py` | `terraform fmt`, pinned toolchain + providers, module sources resolve, described variables/outputs, secrets marked `sensitive`, no committed state |
| `test_k8s_pod_security.py` | Policy baseline across every generated per-Darwin object: namespace scoping, non-root + seccomp + no API token, hardened containers with limits, secrets by reference, default-deny NetworkPolicy |
| `test_observability_stack.py` | Prometheus → Alertmanager → OTel → Jaeger wiring, and every metric an alert rule or Grafana panel queries is emitted by `/metrics` |
| `test_ci_workflows.py` | Every job time-bounded, actions version-pinned, fast tier excludes `slow`, image publish gated on lint + test |
| `test_configuration_contract.py` | Every `serve` flag and every env var read by `src/` is documented in doc 27 / `.env.example` (`make docs-config` regenerates the flag tables) |

### Tier 6: E2E (`tests/e2e/`, markers `e2e` + `slow`)

**What:** Boots `python -m darwin_platform serve` as a subprocess on an ephemeral port and drives it like a deployed client — real TCP, uvicorn, lifespan hooks, the drive loop on wall-clock time. Provider env vars are stripped so the process runs on fakes; a test that wants a real adapter passes its flag explicitly. The harness (`Server`, `server_factory`, `tokens`, `wait_for`) lives in `tests/e2e/conftest.py`.

| File | Covers |
| ---- | ------ |
| `test_serve_lifecycle.py` | spawn → tick → kill → graceful SIGTERM; `kill -9` + restart resumes from SQLite with the audit chain intact; Phase-1/2 wiring flags + the KYC gate |
| `test_auth_and_tenancy.py` | Probes public, API behind bearer auth; owners isolated on every per-Darwin route; operator sees the fleet; restart keeps other tenants locked out |
| `test_owner_journey.py` | Pause freezes the loop and resume restarts it; owner controls round-trip and land in the operator audit; ledger JSON/CSV/summary; WebSocket stream snapshots + live frames |
| `test_python_client.py` | The typed SDK (`darwin_platform.client`) against the live server: preapprovals (operator-only), per-owner freelancer registry, typed errors |

### Tier 7: Performance (`tests/performance/`, markers `performance` + `slow`)

**What:** Two kinds of check that stay deterministic on shared runners. **Scaling** checks compare a small input with a 10× larger one, so machine speed cancels out: O(1) treasury balance, O(limit) audit tail, flat-memory chain boot and Merkle anchor, status latency independent of fleet size. **Budgets** are absolute ceilings with ~10× headroom over a laptop, so an order-of-magnitude regression fails and noise doesn't. `DARWIN_PERF_SCALE=N` multiplies every budget (the slow workflow uses 3).

| File | Covers |
| ---- | ------ |
| `test_audit_chain_perf.py` | Append throughput; boot and Merkle memory flat in chain length; boot verify linear, not quadratic; timeline tail cost independent of chain length |
| `test_genome_hot_paths_perf.py` | Manifest verification per boot; kill-switch poll cost and finality; kill → halt within one heartbeat (< 2 s); O(1) treasury balance; heartbeat throughput on fakes |
| `test_api_latency_perf.py` | p95 budgets for the dashboard's read routes with a 25-Darwin fleet; median spawn; status latency vs fleet size |

**Known ceiling:** with the in-process driver, `events` / `anatomy` / `/metrics` cost O(audit-chain length) because the in-memory sink copies the chain per read. Production runs `--driver k8s`, where the supervisor reads a bounded pod tail; the perf tier measures the dev path at realistic history sizes and documents the ceiling rather than asserting against it.

### Tier 8: 90-day simulation (`tests/simulation/`, marker `slow`)

**What:** A simulated Darwin running for 90 in-sim days, asserting 8 invariants across the entire lifecycle (including reproduction). Files live under `tests/simulation/` and are auto-tagged `slow` by the tier's `conftest.py`, so `pytest -m "not slow"` skips them. Uses `tools/sim/run_sim.py`.

**Invariants asserted:**
1. Darwin survives 90 days without halting (unless killed by design).
2. Ledger reconciles to wallet with zero discrepancy at every checkpoint.
3. Audit chain is unbroken from birth to death.
4. Fitness is non-negative and monotonically non-decreasing over 7-day windows (after warm-up).
5. Kill switch fires within 1 heartbeat of flip.
6. Treasury hard caps are never breached (no single action > $50, no day > $500).
7. Reproduction fires when conditions are met; child boots and loops independently.
8. Mutation archive records all proposals; promoted mutations improve fitness.

**When it runs:** Push to main, weekly cron, manual trigger. ~2–5 minutes wall-clock.

### Soak: 24h soak test (manual / CI-triggered)

**What:** A real Darwin running against real (or compose-level) infrastructure for 24+ hours. Pass criteria defined in doc 20 § "The soak test."

**When it runs:** Before each phase gate. Not part of the normal CI loop — too slow and too expensive.

## Test infrastructure

### Fakes

The `darwin_fakes` package provides deterministic stand-ins:

| Fake | What it replaces | Key property |
| ---- | ---------------- | ------------ |
| `FakeStack` | Full platform bundle | All-in-memory, no I/O |
| `FakeLLM` | LiteLLM transport | Returns deterministic responses per task kind |
| `FakeWallet` | On-chain wallet | In-memory USDC ledger, instant settlement |
| `FakeVault` | HashiCorp Vault | In-memory KV store with tier enforcement |
| `FakeBrowser` | Browserbase | Returns canned page content |
| `FakeSandbox` | E2B microVM | Evaluates trivial code strings |
| `VClock` | `time.time()` | Controllable, deterministic |
| `MemoryAuditSink` | Postgres audit table | In-memory list with same append/read_all interface |
| `SigningService` | KMS / HSM | In-memory Ed25519 keypair |

### Shared fixtures (`conftest.py`)

| Fixture | Provides |
| ------- | -------- |
| `vclock` | A `VClock` instance (deterministic time) |
| `signer` | A `SigningService` (fresh keypair per test) |
| `genesis` | A validly signed generation-0 DNA bundle |
| `booted` | A fully booted `DarwinRuntime` against the fake stack |

### Marks

Tier markers are added automatically by the per-tier `conftest.py` files (`tests/unit/conftest.py`, `tests/property/conftest.py`, …) — you do *not* need to decorate individual test functions with `@pytest.mark.unit` etc. The simulation, e2e, and performance tiers' `conftest.py` additionally add `slow`, so `pytest -m "not slow"` skips them even if an individual file forgot the marker.

| Mark | Source | Meaning | CI behavior |
| ---- | ------ | ------- | ----------- |
| `unit` | auto, `tests/unit/**` | Tier-1 single-module unit test | every push |
| `property` | auto, `tests/property/**` | Tier-2 hypothesis test | every push |
| `redteam` | auto, `tests/redteam/**` | Tier-3 adversarial scenario | every push |
| `integration` | auto, `tests/integration/**` | Tier-4 multi-module flow | every push |
| `infra` | auto, `tests/infra/**` | Tier-5 deployment-asset contract | every push |
| `e2e` | auto, `tests/e2e/**` (also adds `slow`) | Tier-6 real `serve` process | every PR (`e2e` job) + main |
| `performance` | auto, `tests/performance/**` (also adds `slow`) | Tier-7 latency / memory budget | main + weekly |
| `simulation` | auto, `tests/simulation/**` (also adds `slow`) | Tier-8 multi-day sim | main + weekly |
| `slow` | explicit, or via the simulation / e2e / performance tiers | Needs real wall-clock time | main + weekly |
| `asyncio` | explicit (pytest-asyncio) | Async test | normal |

## Coverage gaps and priorities

When deciding what to test next, consult these sources in order:

1. **Doc 24 (threat model)** — any row in the "Why the soma cannot succeed" table without a corresponding red-team test is a gap.
2. **Doc 25 (runbook) failure modes** — each FM-* entry implies at least one test that exercises the detection signal.
3. **The mutation archive** — every rejected patch shape that makes it past initial validation reveals a validator gap worth a test.
4. **Post-mortem entries** (doc 29) — every incident produces a regression test.

## Writing a new test: checklist

1. **Identify the tier.** Unit? Property? Red-team? Integration? Infra? E2E? Performance? Simulation?
2. **Choose the file path.**
   - One module of `src/`: `tests/unit/<package>/<area>/test_<module>.py` (mirrors src).
   - A hypothesis property: `tests/property/test_<property>_properties.py`.
   - An adversarial scenario: `tests/redteam/test_<threat_class>.py` — one file per doc-24 row.
   - A multi-module flow: `tests/integration/test_<flow>.py`.
   - A deployment-asset or config-doc contract: `tests/infra/test_<asset>.py`.
   - Behaviour only visible on the real process (CLI flags, lifespan, restart, sockets): `tests/e2e/test_<journey>.py`, using the `server_factory` fixture.
   - A latency / throughput / memory budget: `tests/performance/test_<area>_perf.py`, using the `perf` fixture. Prefer a scaling ratio (small vs 10× input) over an absolute time; give absolute budgets ~10× headroom.
   - A multi-day in-sim invariant: `tests/simulation/test_<scenario>.py`.
3. **Use fakes, not mocks.** Import from `darwin_fakes`. Mocks hide bugs; fakes behave like real implementations with controlled inputs.
4. **Assert the safety property, not the implementation.** "The kill signal fires" not "the internal counter incremented."
5. **Reference the source doc.** In the docstring, cite which hot path (doc 18), which threat (doc 24), or which failure mode (doc 25) this test covers.
6. **Verify visibility.** If the defense triggers, assert something externally observable happened (audit record, alert, exception, status change).
7. **Keep it fast.** If the test needs > 5s, mark it `@pytest.mark.slow` and justify why. Anything under `tests/simulation/`, `tests/e2e/`, or `tests/performance/` is auto-`slow`.
8. **A bug fix ships its regression test** in the tier where the bug was observable — e.g. the torn-tail audit loss in `JsonlAuditSink` is pinned in `tests/unit/darwin_platform/runtime/test_audit_sink.py`, its tail-read cost in `tests/performance/test_audit_chain_perf.py`.

## Running tests

The repo root [`Makefile`](../../Makefile) wraps the commands below (`make test-fast`, `make test-all`, `make test-unit`, …). Run `make help` for the full list.

```bash
# Fast tier (every push):
uv run pytest -m "not slow"
# or: make test-fast

# Full suite including simulation:
uv run pytest
# or: make test-all

# Just the red-team suite (by path):
uv run pytest tests/redteam -v

# Just the red-team suite (by marker — works wherever the file sits):
uv run pytest -m redteam -v

# Just the property tests:
uv run pytest tests/property -v

# One tier at a time (by path, or by marker with -m):
uv run pytest tests/unit          # make test-unit
uv run pytest tests/integration   # make test-integration
uv run pytest tests/infra -rs     # make test-infra   (-rs shows which CLI checks skipped)
uv run pytest tests/e2e           # make test-e2e
uv run pytest tests/performance   # make test-perf    (DARWIN_PERF_SCALE=3 on a slow box)
uv run pytest tests/simulation    # make test-simulation

# Everything slow (what the slow workflow runs):
uv run pytest -m slow             # make test-slow

# With coverage (pytest-cov is not a project dependency; uv pulls it ad hoc):
uv run --with pytest-cov pytest -m "not slow" \
  --cov=darwin_dna --cov=darwin_platform --cov-report=term-missing
```

## Continuous integration

| Workflow | Trigger | What it runs |
| -------- | ------- | ------------ |
| [`test.yml`](../../.github/workflows/test.yml) | push / PR to `main` and `release/**`; called by `build.yml` | `fast`: `pytest -m "not slow"` (unit, property, red-team, integration, infra) · `e2e`: `pytest -m e2e` · `postgres`: persistence, RLS, and leader election against Postgres 16 |
| [`lint.yml`](../../.github/workflows/lint.yml) | push / PR; called by `build.yml` | `ruff check` (format check is advisory) |
| [`slow.yml`](../../.github/workflows/slow.yml) | push to `main`, Mondays 04:00 UTC, manual | `pytest -m slow` — simulation + e2e + performance, with `DARWIN_PERF_SCALE=3` |
| [`release-gate.yml`](../../.github/workflows/release-gate.yml) | push / PR | `pytest -m redteam` as an explicit release gate (doc 24) |
| [`soak-smoke.yml`](../../.github/workflows/soak-smoke.yml) | push / PR | 60-second shrink of the 24 h soak against a real `serve` process |
| [`helm.yml`](../../.github/workflows/helm.yml) | chart changes | `helm lint` + `helm template` over the default, prod, inline-secret, PDB, and Postgres fixtures |
| [`web.yml`](../../.github/workflows/web.yml) | push / PR | dashboard lint, typecheck, Vitest, build, and Playwright (`dev-token` project) |
| [`build.yml`](../../.github/workflows/build.yml) | push to `main`, `v*.*.*` tags | lint + test (above), then the multi-arch image publish — a red commit never ships an image |
| [`deploy-hetzner.yml`](../../.github/workflows/deploy-hetzner.yml) | published release, manual | helm + `terraform fmt`/`validate` gate, then `terraform apply` behind the `hetzner-prod` environment |
