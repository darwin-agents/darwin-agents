# Darwin tests

One-page map of the test suite. The long-form rationale lives in
`docs/engineering/26-testing-strategy.md`; this file just tells you where
things go and how to run them.

## Layout

```
tests/
├── conftest.py                     # shared fixtures (vclock, signer, genesis, booted)
├── README.md                       # this file
│
├── unit/                           # tier 1 — one module per test, fakes only
│   ├── darwin_dna/
│   │   ├── machinery/              # kill_switch, audit, ledger, vault, treasury,
│   │   │                           #   patch_validator, fitness, memory, neuron_*,
│   │   │                           #   eval_harness, pause, llm_circuit_breaker
│   │   ├── constitution/           # constitution invariants
│   │   ├── evolution/              # reproduction, lineage, compaction, amendments
│   │   ├── neurons/                # per-neuron unit tests
│   │   ├── test_signature.py       # genome signing
│   │   └── test_untrusted.py       # fence wrapping
│   └── darwin_platform/            # mirrors src/darwin_platform/ bounded contexts
│       ├── runtime/                # supervisor, persistence, leadership
│       ├── tenancy/                # provisioning, vault_admin, namespace, cross-pollination
│       ├── payments/               # billing, omnibus, stripe ramp, harvest, sanctions
│       ├── security/               # auth, kyc, signing, key rotation, watermark
│       ├── observability/          # monitoring, drift, merkle anchor, audit, sinks
│       ├── escalations/            # escalation router, FCM push
│       ├── evolution/              # patch staging, eval runner
│       ├── infra/                  # throttles, egress proxy, browser cache
│       ├── adapters/               # wallet, vault, browser, fiat, multichain,
│       │                           #   litellm, stagehand, real-SDK wrappers
│       └── k8s/                    # driver + manifests
│
├── property/                       # tier 2 — hypothesis
│   ├── test_kill_switch_properties.py
│   ├── test_trust_root_properties.py
│   └── test_safety_invariants.py
│
├── redteam/                        # tier 3 — adversarial, mapped to doc 24 rows
│   └── test_redteam.py             # (will be split per-threat-class over time)
│
├── integration/                    # tier 4 — multi-module in-process flows
│   ├── test_boot_integration.py
│   ├── test_runtime_resume.py
│   ├── test_platform_*.py          # supervisor, api, auth, billing, tracing, …
│   ├── test_pathc_*.py             # propose → validate → apply
│   └── test_cross_pollination_*.py # write side, read side, wiring
│
├── simulation/                     # tier 5 — multi-day in-sim runs (slow)
│   ├── test_simulation.py
│   ├── test_simulation_langgraph.py
│   └── test_sim_calibration.py
│
└── fixtures/                       # static test data (manifests, PDFs, sample chains)
```

## Running

```bash
# Fast loop — every push (unit + property + redteam + integration)
uv run pytest -m "not slow"

# Single tier by path
uv run pytest tests/unit
uv run pytest tests/integration

# Single tier by marker (works across paths if you ever move a test)
uv run pytest -m unit
uv run pytest -m property
uv run pytest -m redteam
uv run pytest -m integration
uv run pytest -m simulation        # = -m slow

# Full suite including the slow sim tier (CI on main + nightly)
uv run pytest

# Coverage report on the genome and platform
uv run pytest --cov=darwin_dna --cov=darwin_platform --cov-report=term-missing
```

## Markers

| Marker | What it means | When it runs |
| --- | --- | --- |
| (none) | Implicit unit-tier test | every push |
| `unit` | Auto-applied to `tests/unit/**` | every push |
| `property` | Auto-applied to `tests/property/**` | every push |
| `redteam` | Auto-applied to `tests/redteam/**` | every push |
| `integration` | Auto-applied to `tests/integration/**` | every push |
| `simulation` | Auto-applied to `tests/simulation/**` (also adds `slow`) | nightly + main |
| `slow` | Anything that takes > 5s | nightly + main |
| `asyncio` | Async test (pytest-asyncio) | normal |

Tier markers are added automatically by the per-tier `conftest.py` — you do
not need to decorate test functions with `@pytest.mark.unit` etc.

## Where do I put a new test?

| Kind of test | Goes in |
| --- | --- |
| Single module, fakes only, < 1s | `tests/unit/<package>/<area>/test_<module>.py` |
| Hypothesis / property | `tests/property/test_<property>_properties.py` |
| Adversarial scenario from doc 24 | `tests/redteam/test_<threat_class>.py` |
| Multi-module flow (boot, resume, HTTP, lifecycle) | `tests/integration/test_<flow>.py` |
| Multi-day in-sim invariant | `tests/simulation/test_<scenario>.py` |
| Static data | `tests/fixtures/` |

## Fixtures (`conftest.py`)

| Fixture | Provides |
| --- | --- |
| `vclock` | A `VClock` (deterministic time) |
| `signer` | A `SigningService` (fresh keypair per test) |
| `genesis` | A validly signed generation-0 DNA bundle |
| `booted` | A fully booted `DarwinRuntime` against the fake stack |

The fakes themselves live in `src/darwin_fakes/` — see doc 26 § "Fakes".
