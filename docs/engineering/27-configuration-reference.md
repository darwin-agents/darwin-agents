# Configuration reference

Every configuration knob for the Darwin platform in one place. CLI flags take precedence over environment variables. When neither is set, fakes are used (safe for development, deterministic for testing).

## CLI subcommands

```
darwin-platform serve    # Run the FastAPI supervisor
darwin-platform signing-key  # Print the platform's signing public key (PEM)
```

## `serve` — core flags

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--host` | — | `127.0.0.1` | Bind address. Set `0.0.0.0` for containers / k8s. |
| `--port` | — | `8000` | Bind port. |
| `--db` | — | `None` (in-memory) | Persistence backend. Path → SQLite file. `postgresql://` DSN → Postgres mode. |
| `--auth-tokens` | — | `None` (trust-all) | JSON file seeding the bearer token store. **Never expose the API publicly without this.** |
| `--log-level` | — | `info` | Uvicorn log level: `critical`, `error`, `warning`, `info`, `debug`, `trace`. |
| `--leader-election` | — | off | Enable HA via Postgres advisory locks. Requires `--db` to be a Postgres DSN. |
| `--leader-lock-key` | — | supervisor default | Override the lock BIGINT to isolate multiple platforms sharing one Postgres. |

## `serve` — real wallet (doc 17)

When these flags are set, `RealWallet` replaces `FakeWallet`. Requires the `[web3]` extra.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--rpc-url` | `DARWIN_RPC_URL` | — | EVM HTTP RPC (e.g. `https://sepolia.base.org`). |
| `--wallet-key` | `DARWIN_WALLET_KEY` | — | Hex private key for on-chain USDC. **Never log.** |
| `--usdc-contract` | — | Base constants | USDC ERC-20 contract address override. |
| `--chain-networks` | — | `base` | Comma-separated: `base,solana,stellar`. Preferred order for MultiChainRouter. |
| `--solana-rpc-url` | `DARWIN_SOLANA_RPC_URL` | — | Solana HTTP RPC endpoint. |
| `--solana-wallet-key` | `DARWIN_SOLANA_WALLET_KEY` | — | Solana private key (hex). |
| `--stellar-horizon-url` | `DARWIN_STELLAR_HORIZON_URL` | — | Stellar Horizon endpoint. |
| `--stellar-secret` | `DARWIN_STELLAR_SECRET` | — | Stellar account secret. |

**Interactions:**
- At least one chain (base, solana, or stellar) must have both its RPC + key set.
- When multiple chains are enabled, the wallet uses `MultiChainRouter` with the preferred order from `--chain-networks`.
- x402 (`X402PaymentClient`) is automatically wired when any real wallet is active.

## `serve` — real vault (doc 16)

When both are set, `RealVaultBackend` replaces `FakeVault`. Requires the `[hvac]` extra.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--vault-url` | `DARWIN_VAULT_URL` | — | HashiCorp Vault address. |
| `--vault-token` | `DARWIN_VAULT_TOKEN` | — | Vault token with read+write ACL. |
| `--vault-mount` | — | `secret` | KV v2 mount point. |

## `serve` — real browser (doc 15)

When either is set, `BrowserUseClient` replaces `FakeBrowser`. Requires the `[browser]` extra.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--browserbase-url` | `BROWSERBASE_URL` | — | Browserbase WS endpoint. |
| `--browserbase-api-key` | `BROWSERBASE_API_KEY` | — | Browserbase API key. |

## `serve` — real sandbox (doc 14)

When set, `E2BSandbox` replaces `FakeSandbox`. Requires the `[e2b]` extra.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--e2b-api-key` | `E2B_API_KEY` | — | E2B API key. |

## `serve` — compliance (doc 17)

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--sanctions-provider` | — | `local` | Provider: `local`, `chainalysis`, or `trm`. |
| `--sanctions-api-key` | `DARWIN_SANCTIONS_API_KEY` | — | API key for chainalysis/trm. Required when provider ≠ local. |
| `--sanctions-denylist` | — | — | JSON file path for `LocalDenylistScreener`. Format: `{"denylist": [...], "denied_prefixes": [...]}`. |

**Interactions:**
- `--sanctions-provider chainalysis` or `trm` requires `--sanctions-api-key`.
- `--sanctions-provider local` uses `--sanctions-denylist` (or no screening if omitted).
- Screening runs before every outbound wallet transfer regardless of provider choice.

## `serve` — notification sinks (doc 08)

Multiple sinks can be active simultaneously (fan-out).

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--slack-webhook-url` | `DARWIN_SLACK_WEBHOOK_URL` | — | Slack incoming-webhook for escalation notifications. |
| `--escalation-webhook-url` | `DARWIN_ESCALATION_WEBHOOK_URL` | — | Generic JSON-POST webhook for escalation tickets. |
| `--alert-webhook-url` | `DARWIN_ALERT_WEBHOOK_URL` | — | Generic JSON-POST webhook for monitor alerts. |
| `--pagerduty-routing-key` | `DARWIN_PAGERDUTY_ROUTING_KEY` | — | PagerDuty Events v2 routing key for CRITICAL alerts. |

## `serve` — K8s driver (doc 08 production posture)

When `--driver=k8s`, the supervisor provisions per-Darwin Kubernetes namespaces instead of running Darwins as asyncio tasks.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--driver` | — | `in-process` | `in-process` = asyncio tasks. `k8s` = per-Darwin namespace pods. |
| `--k8s-namespace-prefix` | — | `darwin-` | Namespace naming: `{prefix}{darwin_id}`. |
| `--k8s-runtime-class` | — | `kata` | Pod RuntimeClass (kata, gvisor, runc). |
| `--k8s-orchestrator-image` | — | supervisor image | Container image for the orchestrator pod. |
| `--k8s-egress-proxy-namespace` | — | `platform-data` | Namespace hosting the egress proxy (NetworkPolicy peer). |
| `--k8s-lancedb-image` | — | `lancedb/lancedb:latest` | Per-Darwin LanceDB sidecar image. |
| `--k8s-lancedb-storage-size` | — | `1Gi` | PVC size for LanceDB data. |
| `--k8s-lancedb-storage-class` | — | cluster default | StorageClass for LanceDB PVC. |
| `--k8s-postgres-namespace` | — | — | Postgres namespace for NetworkPolicy egress. |
| `--k8s-postgres-pod-label` | — | — | Postgres pod-label selector (`key=value`). |
| `--k8s-postgres-port` | — | `5432` | Postgres port. |
| `--k8s-database-url-secret` | — | — | Secret reference for orchestrator's `DATABASE_URL`. Format: `name[:key]`. |

**Interactions:**
- `--driver=k8s` is incompatible with real-adapter flags (`--rpc-url`, etc.). Those belong in the orchestrator image's env, not the supervisor.
- `--driver=k8s` still requires `--db` (Postgres) for shared platform state.
- `--leader-election` is recommended with `--driver=k8s` for supervisor HA.

## `signing-key` subcommand

| Flag | Default | Description |
| ---- | ------- | ----------- |
| `--db` | `None` | Optional SQLite file (opens the same persistence the supervisor would). |

## Genome hyperparameters (Layer 0 constants)

These live in `src/darwin_dna/hyperparameters.py` and are **not** CLI-configurable — they're part of the immutable DNA. Documented here for reference.

### Hard caps (inviolable)

| Parameter | Value | Enforced by |
| --------- | ----- | ----------- |
| `per_action_usdc` | $50 | Treasury policy engine + wallet-level policy |
| `per_day_usdc` | $500 | Treasury policy engine |
| `per_lifetime_usdc` | $10,000 | Treasury policy engine |

### Hyperparameters (tunable by soma within bounds)

| Parameter | Default | Range / constraint |
| --------- | ------- | ------------------ |
| `heartbeat_interval_seconds` | 30 | [5, 600] |
| `min_dispatches_before_mutation` | 100 | ≥ 1 |
| `min_recurring_failures_for_mutation` | 3 | ≥ 1 |
| `min_profit_for_reproduction_usdc` | $25 | ≥ 0 |
| `profit_transfer_fraction` | 0.5 | (0, 1) |
| `reproduction_fitness_window_days` | 7 | ≥ 1 |
| `generation_soft_cap` | 20 | ≥ 2 |
| `fitness_profit_weight` | 1.0 | Fixed (DNA) |
| `fitness_diversity_weight` | 0.1 | Fixed (DNA) |
| `fitness_escalation_quality_weight` | 0.05 | Fixed (DNA) |
| `fitness_policy_violation_penalty` | 10.0 | Fixed (DNA) |
| `tier_coding_max_daily_calls` | 20 | ≥ 1 |
| `tier_reasoning_max_daily_calls` | 2000 | ≥ 1 |
| `tier_fast_max_daily_calls` | 50,000 | ≥ 1 |
| `minimum_viable_balance_usdc` | $1 | ≥ 0 |
| `revenue_grace_window_seconds` | 86,400 (24h) | ≥ 0 |

## Monitor quotas (platform-side)

Configured in `src/darwin_platform/monitoring.py`. Currently code constants; will become per-fleet operator config in Phase 2.

| Quota | WARN | CRITICAL |
| ----- | ---- | -------- |
| `spend_velocity_usd_per_hour` | $100 | $300 |
| `failure_rate` (lifetime) | 30% | 60% |
| `heartbeat_gap_seconds` | 1,200 | 3,000 |
| `escalation_rate_per_hour` | 5 | 20 |
| `spread_spend_count_per_hour` | 30 (below $1 floor) | 80 |
| `dedup_window_seconds` | 60 | — |

## Docker Compose environment

The `docker/.env` file configures the compose stack:

| Variable | Service | Purpose |
| -------- | ------- | ------- |
| LLM provider keys (e.g. `OPENAI_API_KEY`) | litellm | Passed to LiteLLM proxy for real model access |
| `DARWIN_RPC_URL` | supervisor | Optional: real wallet (if set in compose override) |
| `DARWIN_WALLET_KEY` | supervisor | Optional: real wallet signing |

See [`docker/README.md`](../../docker/README.md) for the complete compose setup.

## Helm values (summary)

The full values reference is in [`deploy/helm/darwin-platform/README.md`](../../deploy/helm/darwin-platform/README.md). Key values:

| Value path | Maps to | Notes |
| ---------- | ------- | ----- |
| `database.url` / `database.existingSecret` | `--db` | Postgres DSN or Secret reference |
| `leaderElection.enabled` | `--leader-election` | Requires Postgres |
| `auth.enabled` / `auth.existingSecret` | `--auth-tokens` | Secret holding `tokens.json` |
| `tracing.endpoint` | OTEL env vars | Injected as container env |
| `serviceMonitor.enabled` | Prometheus scrape | Requires prometheus-operator |
| `networkPolicy.enabled` | K8s NetworkPolicy | Default-deny egress + selective allow |

## Precedence rules

1. CLI flag (highest priority).
2. Environment variable (if documented as a fallback for that flag).
3. Code default (lowest; always safe for development).

## What happens when nothing is configured

With zero flags and zero env vars, the supervisor starts with:
- In-memory state (no persistence — lost on restart).
- FakeWallet, FakeVault, FakeBrowser, FakeSandbox, FakeLLM.
- No auth (trust every caller).
- No sanctions screening.
- No notifications (alerts go to structlog only).
- In-process driver (Darwins are asyncio tasks).

This is the test/CI mode. Fully deterministic, zero external dependencies, zero secrets needed.
