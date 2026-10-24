# Configuration reference

Every configuration knob for the Darwin platform in one place: CLI flags, their environment-variable fallbacks, platform-internal environment, genome constants, and deployment values. CLI flags take precedence over environment variables. When neither is set, in-memory fakes are used — safe for development, deterministic for tests.

> The [`serve` flag reference](#serve-flag-reference) below is **generated** from the CLI's argparse definitions by [`scripts/gen_config_reference.py`](../../scripts/gen_config_reference.py) (`make docs-config`). `tests/infra/test_configuration_contract.py` fails when a flag or an environment variable read by `src/` is undocumented, so this page cannot silently drift from the code.

## CLI subcommands

```
darwin-platform serve        # Run the FastAPI supervisor
darwin-platform signing-key  # Print the platform's signing public key (PEM)
```

`signing-key` takes a single optional `--db` (the same SQLite file the supervisor would open).

## Choosing providers

Each external dependency has an in-memory fake (the default) and one or more real adapters selected by flags. The flag tables are in the [reference](#serve-flag-reference); this section explains the choices that are not obvious from a single flag.

### Wallet (`--wallet-provider`, doc 17)

`RealWallet` replaces `FakeWallet` whenever a wallet provider is selected. Production must set one; an unset provider is the dev-only `FakeWallet`.

| Provider | Custody | Notes |
| -------- | ------- | ----- |
| `cdp` | Coinbase CDP account per Darwin; key TEE-held by Coinbase | **Production choice.** Requires the `[cdp]` extra and `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` / `CDP_WALLET_SECRET`. `--cdp-per-tx-usdc` / `--cdp-per-day-usdc` add adapter-level caps below the treasury policy (doc 24 defense-in-depth). In k8s mode each pod gets a **watch-only** wallet; signing stays in the supervisor, and `--gas-tank-key` tops up each Darwin's gas. |
| `local-vault` | Self-custodial keypair minted at spawn, sealed at `sensitive/wallet/<chain>/secret` in the Darwin's Vault namespace | Needs only RPC URLs (`--rpc-url`, `--solana-rpc-url`, `--stellar-horizon-url`) and the matching extras. |
| `web3` | One shared private key (`--wallet-key`) for every Darwin | **Testnet-only escape hatch.** Refused with `--network mainnet`. With no key in the environment it falls back to `FakeWallet` (what the e2e tier and the Compose stack use). |
| `watch` | Read-only: observes `--wallet-address` | Books inbound revenue/deposits without holding a key. |

Cross-provider behaviour:

- `--network` is **the single authoritative money switch** (doc 30): `mainnet` selects the mainnet USDC contract and chain id, enables on-chain confirmation gating, refuses the shared-key `web3` wallet, and (with `--env`) implies production. Nothing guesses the network from an RPC URL.
- With several `--chain-networks`, the wallet routes through `MultiChainRouter` in the listed preference order.
- x402 (`X402PaymentClient`) is wired automatically whenever a real wallet is active.
- The supervisor forwards `--wallet-provider` to every per-Darwin orchestrator pod, so the whole fleet uses one backend.
- `--wallet-state-dir` gives each Darwin a durable `<id>.wallet.jsonl` feed + accrual log; `--platform-fee-address` is where metering fees settle (must be a checksummed `0x` address in production).

### Authentication (doc 08)

| Mode | Configure with | Use for |
| ---- | -------------- | ------- |
| None | nothing | Tests and local dev only — the API trusts every caller. |
| Static bearer tokens | `--auth-tokens tokens.json` (shape: [`infra/docker/tokens.example.json`](../../infra/docker/tokens.example.json)) | Dev and CI. Ownership comes from each token's `owned_darwins` list plus Darwins spawned during the process lifetime. Production boots flag it as a problem. |
| Clerk JWTs | `--clerk-issuer` (or `--clerk-jwks-url`), optional `--clerk-audience` | Production. RS256 tokens verified against Clerk's JWKS; ownership is re-seeded from the persisted `owner_subject` after a restart. |

Scopes are `owner` (per-Darwin actions on owned Darwins) and `platform_operator` (whole fleet). `/healthz`, `/readyz`, and `/metrics` are unauthenticated for probes and scrapers.

### KYC (`--kyc-provider`, doc 17)

`stub` approves through the `/platform/kyc/*` flow (dev/CI); `persona` and `onfido` call the real providers and verify their signed webhooks (`--kyc-persona-webhook-secret`, `--kyc-onfido-webhook-token`). With a provider wired, spawn and the fiat paths are gated until the owner is verified.

### Fiat ramp (`--fiat-provider`, doc 17)

`moonpay` runs the hosted-widget flow with signed-webhook settlement into the omnibus address (`--moonpay-omnibus-address`); production accepts only `none` or `moonpay` — the platform never holds fiat. `stripe` (card/ACH deposit + Connect payouts) is available for non-production setups.

### LLM gateway (doc 13)

`--litellm-url` + `--litellm-master-key` route every model call through the LiteLLM proxy instead of `FakeLLM`. `--litellm-virtual-keys` mints a per-Darwin virtual key (optional `--litellm-virtual-key-budget-usdc`) so the master key is only an admin credential and the proxy meters spend per Darwin; the reconciler compares that spend with the ledger within `--litellm-spend-tolerance-usdc`.

### Kubernetes driver (`--driver k8s`, doc 08)

The supervisor provisions a namespace per Darwin (`{--k8s-namespace-prefix}{darwin_id}`) with a durable data volume, a soma volume, a NetworkPolicy, and the orchestrator pod, instead of running Darwins as asyncio tasks.

- Still requires `--db` (Postgres) for shared platform state; `--leader-election` is recommended for HA.
- Real-adapter settings are forwarded to pods (sealed in each pod's control Secret), not used by the supervisor in-process. Pods never receive signing keys or the shared Vault token — with `--tenant-namespaces` each pod gets its own Vault AppRole and organism database (`--organism-db-admin-url`).
- `POD_NAMESPACE` (downward API) tells the supervisor which namespace its NetworkPolicy peers live in.

### Tenant isolation (Postgres only)

With a `postgresql://` DSN, schema v12 enables row-level security on every `darwin_id`-keyed platform table (doc 23). The control-plane DB role **must** be a superuser or carry `BYPASSRLS` — otherwise `FORCE ROW LEVEL SECURITY` gates the supervisor itself and it sees no rows. The per-Darwin data path (dashboard read-only window, scoped readers) goes through the restricted `darwin_tenant` role provisioned by `PersistenceStore.ensure_tenant_role()`. SQLite has no RLS — single-process dev only.

### Sanctions screening (doc 17)

`--sanctions-provider local` (default) screens against `--sanctions-denylist` plus any `--sanctions-list-file` dumps (OFAC SDN / EU / UK). `chainalysis` and `trm` require `--sanctions-api-key`. Screening runs before every outbound wallet transfer regardless of provider.

## `serve` flag reference

<!-- BEGIN GENERATED: serve flags (scripts/gen_config_reference.py) -->

### `serve` — core

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--host` | — | `127.0.0.1` | Bind address (default: 127.0.0.1; set 0.0.0.0 for k8s) |
| `--port` | — | `8000` | Bind port (default: 8000) |
| `--heartbeat-seconds` | — | `5.0` | Gap between per-Darwin heartbeats (default: 5.0). The supervisor library default is 0.001 (tuned for fast tests); ``serve`` overrides to a production-shaped pace so the audit chain doesn't bloat by millions of records per hour and the spread-spend monitor's production thresholds make sense in a sim. Drop to 0.001 for a soak; raise to 60+ to match a slower real-world cadence. |
| `--db` | — | — | SQLite file for restart-survivable state. Omitted = in-memory. |
| `--soma-root` | — | — | Directory root for per-Darwin soma trees (docs 03/21/05). When set, apply_mutation writes promoted patches to the live soma on disk (real self-modification, not the Phase-0 simulation) AND a death-time forensic snapshot is captured per Darwin under &lt;soma-root&gt;/_forensics/&lt;id&gt;. Omitted = self-modification stays simulated and no soma forensics are captured. |
| `--auth-tokens` | — | — | JSON file of bearer tokens to seed the token store. Without this, the API trusts every caller — DO NOT expose publicly. |
| `--log-level` | — | `info` | One of: `critical`, `error`, `warning`, `info`, `debug`, `trace`. |
| `--cors-origin` | — | — | Allow this origin to make CORS requests. Repeat for multiple (e.g. --cors-origin http://localhost:3000 --cors-origin https://app.example.com). Required when the Next.js dashboard runs on a separate port; without it the browser preflight OPTIONS gets a 405. Defaults to empty — same-origin only. |
| `--enable-docs` | — | off | Expose interactive API docs (Swagger UI at /docs, ReDoc at /redoc, OpenAPI JSON at /openapi.json). Off by default — this surface is meant to sit behind the platform edge/auth. When the flag is omitted, the DARWIN_ENABLE_DOCS env var is honored (1/true/yes/on). Use for local dev only. |
| `--leader-election` | — | off | Enable HA leader election via Postgres advisory locks. Requires --db to be a postgres:// DSN. Exactly one pod holds the lock and drives Darwins; others stay warm + report /readyz=503 until they win. |
| `--leader-lock-key` | — | — | Override the default lock key (BIGINT). Used to isolate platform releases sharing one Postgres instance. |
| `--env` | `DARWIN_ENV` | `dev` | One of: `dev`, `staging`, `production`. Deployment environment. Env: DARWIN_ENV (default: dev). In 'production' (or with --network mainnet) the in-memory dev fakes for safety-critical backends are refused — a real E2B sandbox is required (doc 14). |
| `--egress-proxy-url` | `DARWIN_EGRESS_PROXY_URL` | — | Base URL of the platform egress proxy. When set, kill + quarantine POST /revoke so a Darwin's outbound traffic is cut at the choke-point in parallel with the kill flag (docs 08/09/24). Env: DARWIN_EGRESS_PROXY_URL |

### `serve` — Clerk auth (doc 08)

Verify control-plane bearer tokens as Clerk-issued RS256 JWTs against Clerk's JWKS. An alternative to --auth-tokens; both are optional (no auth configured = trust every caller — dev only).

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--clerk-issuer` | `CLERK_ISSUER` | — | Clerk issuer URL — the JWKS is derived as &lt;issuer&gt;/.well-known/jwks.json. Env: CLERK_ISSUER |
| `--clerk-jwks-url` | `CLERK_JWKS_URL` | — | Clerk JWKS URL (overrides the issuer-derived default). Env: CLERK_JWKS_URL |
| `--clerk-audience` | `CLERK_AUDIENCE` | — | Expected JWT 'aud' claim (optional). Env: CLERK_AUDIENCE |

### `serve` — real wallet (doc 17)

Connect to a real EVM chain. Requires the [web3] extra. When omitted the in-memory FakeWallet is used.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--rpc-url` | `DARWIN_RPC_URL` | — | EVM HTTP RPC endpoint (e.g. https://sepolia.base.org). Env: DARWIN_RPC_URL |
| `--wallet-key` | `DARWIN_WALLET_KEY` | — | Hex private key for on-chain USDC signing. Env: DARWIN_WALLET_KEY. Never log this value. |
| `--store-price-usdc` | `DARWIN_STORE_PRICE_USDC` | — | Open the x402 storefront: every Darwin sells a research report for this price, paid to its own wallet. Env: DARWIN_STORE_PRICE_USDC |
| `--x402-facilitator-url` | `DARWIN_X402_FACILITATOR_URL` | — | x402 facilitator (verify/settle). Default: x402.org on testnet, Coinbase's CDP facilitator on mainnet. Env: DARWIN_X402_FACILITATOR_URL |
| `--gas-tank-key` | `DARWIN_GAS_TANK_KEY` | — | Private key of a small ETH float that tops up each Darwin's CDP account with gas before the platform signs for it (--driver k8s). Env: DARWIN_GAS_TANK_KEY |
| `--wallet-address` | `DARWIN_WALLET_ADDRESS` | — | Address a --wallet-provider watch wallet reads (no key; the platform signs). Env: DARWIN_WALLET_ADDRESS |
| `--platform-fee-address` | `DARWIN_PLATFORM_FEE_ADDRESS` | — | Platform USDC address that accrued metering + harvest fees settle to (one transfer per maintenance pass). Env: DARWIN_PLATFORM_FEE_ADDRESS |
| `--wallet-state-dir` | `DARWIN_WALLET_STATE_DIR` | — | Directory for each real wallet's durable feed + accrual log (&lt;darwin_id&gt;.wallet.jsonl). Unset = in memory (lost on restart). Env: DARWIN_WALLET_STATE_DIR |
| `--usdc-contract` | — | — | USDC ERC-20 contract address. Defaults to Base Sepolia or mainnet constant based on the RPC URL. |
| `--chain-networks` | — | `base` | Comma-separated list of chains to enable, in preferred order: base, solana, stellar. Default: base only. With multiple chains, transfers route via MultiChainRouter. |
| `--solana-rpc-url` | `DARWIN_SOLANA_RPC_URL` | — | Solana HTTP RPC endpoint. Env: DARWIN_SOLANA_RPC_URL |
| `--solana-wallet-key` | `DARWIN_SOLANA_WALLET_KEY` | — | Solana wallet private key. Env: DARWIN_SOLANA_WALLET_KEY |
| `--stellar-horizon-url` | `DARWIN_STELLAR_HORIZON_URL` | — | Stellar Horizon RPC endpoint. Env: DARWIN_STELLAR_HORIZON_URL |
| `--stellar-secret` | `DARWIN_STELLAR_SECRET` | — | Stellar account secret. Env: DARWIN_STELLAR_SECRET |
| `--wallet-provider` | — | — | One of: `web3`, `cdp`, `local-vault`, `watch`. Wallet backend (default: none = in-memory FakeWallet, dev only; production must pick one). 'cdp' (Coinbase CDP, non-custodial, per-Darwin TEE-held key — production default); 'local-vault' (per-Darwin keypair minted on spawn and sealed in the per-Darwin vault — self-custodial for chains CDP doesn't cover); 'web3' (single shared private key from env — testnet dev shortcut; refuses mainnet RPC URLs). |
| `--cdp-api-key-id` | `CDP_API_KEY_ID` | — | Coinbase CDP API key id (when --wallet-provider cdp). Env: CDP_API_KEY_ID |
| `--cdp-api-key-secret` | `CDP_API_KEY_SECRET` | — | Coinbase CDP API key secret (when --wallet-provider cdp). Env: CDP_API_KEY_SECRET. Never log this value. |
| `--cdp-wallet-secret` | `CDP_WALLET_SECRET` | — | Coinbase CDP wallet secret for transaction signing. Env: CDP_WALLET_SECRET |
| `--network` | — | `testnet` | One of: `testnet`, `mainnet`. Authoritative chain network for the whole platform (CDP wallet, USDC contract, omnibus, money-safety gate). Default: testnet. --network mainnet is the single explicit switch to real money. |
| `--cdp-mainnet` | — | off | DEPRECATED alias for --network mainnet (kept for back-compat). |
| `--cdp-per-tx-usdc` | — | — | Independent per-transaction USDC cap enforced at the CDP wallet adapter (doc 24 defense-in-depth, below the treasury policy). |
| `--cdp-per-day-usdc` | — | — | Independent per-day USDC cap enforced at the CDP wallet adapter. |

### `serve` — real vault (doc 16)

Connect to HashiCorp Vault. Requires the [hvac] extra. When omitted the in-memory FakeVault is used.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--vault-url` | `DARWIN_VAULT_URL` | — | HashiCorp Vault address (e.g. https://vault.example.com:8200). Env: DARWIN_VAULT_URL |
| `--vault-token` | `DARWIN_VAULT_TOKEN` | — | Vault token with read+write on the KV mount. Env: DARWIN_VAULT_TOKEN |
| `--vault-mount` | — | `secret` | KV v2 mount point (default: secret). |
| `--vault-auth` | — | `token` | One of: `token`, `approle`, `kubernetes`. Vault auth method. 'token' is the dev path (--vault-token); 'approle' and 'kubernetes' are the production methods (leased tokens, auto-renewed on expiry). Auto-selected when only AppRole / k8s creds are supplied. |
| `--vault-role-id` | `DARWIN_VAULT_ROLE_ID` | — | AppRole role id (auth_method=approle). Env: DARWIN_VAULT_ROLE_ID |
| `--vault-secret-id` | `DARWIN_VAULT_SECRET_ID` | — | AppRole secret id (auth_method=approle). Env: DARWIN_VAULT_SECRET_ID. Never log this value. |
| `--vault-k8s-role` | `DARWIN_VAULT_K8S_ROLE` | — | Vault Kubernetes-auth role (auth_method=kubernetes). Env: DARWIN_VAULT_K8S_ROLE |
| `--vault-k8s-jwt-path` | — | — | Service-account JWT path for k8s auth (default: the in-pod /var/run/secrets/kubernetes.io/serviceaccount/token). |

### `serve` — tenant namespaces (doc 22)

Per-Darwin infrastructure isolation: a Vault policy/AppRole + a private organism Postgres database, provisioned at spawn and torn down (after a 90-day retention) on death. Requires --vault-url + --vault-token and --organism-db-admin-url.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--tenant-namespaces` | — | off | Construct the NamespaceProvisioner and provision a per-Darwin Vault namespace + organism DB on every spawn. Off by default (the in-process FakeVault path-prefix model stays the default). |
| `--organism-db-admin-url` | `DARWIN_ORGANISM_DB_ADMIN_URL` | — | Admin Postgres DSN for the shared organism cluster (needs CREATEDB + CREATEROLE). Env: DARWIN_ORGANISM_DB_ADMIN_URL. URL-style DSNs only (postgresql://user:pw@host:port/db). |
| `--organism-db-child-host` | — | — | Host to embed in each Darwin's organism DSN (default: parsed from --organism-db-admin-url). Set when pods reach Postgres at a different address than the admin connection does. |
| `--organism-db-child-port` | — | — | Port to embed in each Darwin's organism DSN (default: parsed from --organism-db-admin-url). |
| `--organism-db-sslmode` | — | — | Optional sslmode appended to each Darwin's organism DSN (e.g. require). |
| `--organism-db-no-readonly` | — | off | Skip the read-only dashboard role (by default each organism DB gets a second SELECT-only LOGIN role for the dashboard's data browser). |

### `serve` — treasury price feed (doc 05)

Mark a trading Darwin's non-USDC holdings to live USDC prices so the bankruptcy check sees net worth, not just cash. Omitted → holdings are valued at cost basis (safe, not live).

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--price-feed-url` | `DARWIN_PRICE_FEED_URL` | — | HTTP price-feed endpoint (CoinGecko simple/price-shaped). Env: DARWIN_PRICE_FEED_URL. |
| `--price-feed-ttl-seconds` | — | `60.0` | How long a quote is cached before re-fetching (default 60s; perceive marks holdings every heartbeat). |

### `serve` — real browser (doc 15)

Connect to Browserbase. Requires the [browser] extra. When omitted the in-memory FakeBrowser is used.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--browser-provider` | — | `browserbase` | One of: `browserbase`, `browser-use-cloud`. Real browser substrate (doc 31). 'browserbase' wires the OSS browser-use library against Browserbase (default). 'browser-use-cloud' wires the V3 Agent API against Browser Use Cloud's Stealth Browsers (requires [browser-use-cloud] extra). |
| `--browserbase-url` | `BROWSERBASE_URL` | — | Browserbase WS endpoint. Env: BROWSERBASE_URL |
| `--browserbase-api-key` | `BROWSERBASE_API_KEY` | — | Browserbase API key. Env: BROWSERBASE_API_KEY |
| `--browserbase-live-view-url` | `BROWSERBASE_LIVE_VIEW_URL` | — | Browserbase session live-view (debug) URL, surfaced to the owner on a captcha/human-verification wall so they can take over the live session (doc 15 §human takeover). Env: BROWSERBASE_LIVE_VIEW_URL |
| `--browser-use-api-key` | `BROWSER_USE_API_KEY` | — | Browser Use Cloud API key (when --browser-provider browser-use-cloud). Env: BROWSER_USE_API_KEY |
| `--browser-use-model` | — | — | Optional model name passed to Browser Use Cloud — e.g. a BU custom model (doc 31). Default: cloud picks. |
| `--browser-use-max-cost-usdc` | — | `0.1` | Per-session spend cap for the Browser Use Cloud V3 Agent. Pre-debited from the wallet and passed to the SDK as max_cost_usd so cloud billing matches the ledger debit. Default: 0.10 |

### `serve` — real sandbox (doc 14)

Connect to E2B for code-running neurons. Requires the [e2b] extra. When omitted the in-memory FakeSandbox is used.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--e2b-api-key` | `E2B_API_KEY` | — | E2B API key. Env: E2B_API_KEY |

### `serve` — compliance (doc 17)

Sanctions screening run before every wallet outbound transfer. Default = no screener (development); production wires the OFAC denylist or a real provider (Chainalysis, TRM Labs) via the SanctionsScreener Protocol.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--sanctions-denylist` | — | — | Path to a JSON file: {"denylist": ["0xdead..."], "denied_prefixes": ["0xff"]}. Loaded into a LocalDenylistScreener at startup. |
| `--sanctions-list-file` | — | — | Path to a raw sanctions-list dump (OFAC SDN / EU Consolidated / UK HMT) of crypto addresses. Repeatable. Ingested + merged into the LocalDenylistScreener at startup so the offline screener screens against real addresses, not an empty set. |
| `--sanctions-provider` | — | `local` | One of: `local`, `chainalysis`, `trm`. Sanctions screening provider. 'local' uses --sanctions-denylist; 'chainalysis' / 'trm' use the real provider's HTTP API (requires --sanctions-api-key). |
| `--sanctions-api-key` | `DARWIN_SANCTIONS_API_KEY` | — | API key for the chosen sanctions provider. Env: DARWIN_SANCTIONS_API_KEY |

### `serve` — notification sinks (doc 08)

Where escalation tickets + monitor alerts are sent. Multiple flags fan out — Slack + webhook + email are all delivered.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--slack-webhook-url` | `DARWIN_SLACK_WEBHOOK_URL` | — | Slack incoming-webhook URL for escalation notifications. Env: DARWIN_SLACK_WEBHOOK_URL |
| `--escalation-webhook-url` | `DARWIN_ESCALATION_WEBHOOK_URL` | — | Generic JSON-POST webhook for escalation tickets. Env: DARWIN_ESCALATION_WEBHOOK_URL |
| `--alert-webhook-url` | `DARWIN_ALERT_WEBHOOK_URL` | — | Generic JSON-POST webhook for monitor alerts. Env: DARWIN_ALERT_WEBHOOK_URL |
| `--pagerduty-routing-key` | `DARWIN_PAGERDUTY_ROUTING_KEY` | — | PagerDuty Events v2 routing key for critical monitor alerts. Env: DARWIN_PAGERDUTY_ROUTING_KEY |

### `serve` — observability (doc 08)

OpenTelemetry trace export. Without this, spans stay NoOp.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--otlp-endpoint` | `OTEL_EXPORTER_OTLP_ENDPOINT` | — | OTLP/HTTP collector base URL (e.g. http://jaeger:4318). Spans the supervisor emits are exported here. Env: OTEL_EXPORTER_OTLP_ENDPOINT |

### `serve` — LLM gateway (doc 13)

Point the genome's LLM transport at a real LiteLLM proxy. Without this, the deterministic FakeLLM is used.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--litellm-url` | `DARWIN_LITELLM_URL` | — | LiteLLM proxy base URL (e.g. http://litellm:4000). Env: DARWIN_LITELLM_URL |
| `--litellm-master-key` | `DARWIN_LITELLM_MASTER_KEY` | — | LiteLLM proxy master key (default: dev_only_master_key, matching infra/docker/litellm.config.yaml). Env: DARWIN_LITELLM_MASTER_KEY |
| `--litellm-virtual-keys` | `DARWIN_LITELLM_VIRTUAL_KEYS` | off | Mint a per-Darwin LiteLLM virtual key at spawn (via the proxy's /key/generate admin API) instead of every Darwin sharing the master key (doc 13). Requires --litellm-url; the master key is used only as the admin credential. Each key is scoped to the darwin_id, sealed in the Darwin's vault, and revoked on death; the proxy's per-key spend table is reconciled against each Darwin's ledger every maintenance cycle. Env: DARWIN_LITELLM_VIRTUAL_KEYS=1 |
| `--litellm-virtual-key-budget-usdc` | — | — | Optional hard per-key spend ceiling (max_budget) applied to each minted virtual key. The proxy returns 429 once a key exceeds it. Omit for no proxy-side ceiling (the wallet's own caps still apply). |
| `--litellm-spend-tolerance-usdc` | — | `0.05` | Drift tolerance for the LiteLLM spend reconciler (default 0.05). Proxy-vs-ledger drift beyond this pages on-call. |

### `serve` — Phase 1 evolution (doc 21)

HSM-backed DNA signing + sandboxed patch staging.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--signing-vault-transit-key` | — | — | Vault Transit key name for HSM-backed DNA signing (reuses --vault-url / --vault-token). Without this, an in-process Ed25519 signer is used. |

### `serve` — Phase 2 services (doc 22)

Optional platform services: drift detection, Merkle anchoring, omnibus wallet, KYC, FCM push. Each is off unless its flag is set. The maintenance interval drives their periodic hooks.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--enable-drift-detection` | — | off | Run the behavioural drift detector (rolling 7-window profile, 2σ alerts). |
| `--enable-resource-anomaly-detection` | — | off | Run the resource-consumption anomaly detector (per-Darwin LLM-spend / sandbox / browser / egress vs its own baseline; WARN at 2σ, CRITICAL at 3σ or sustained, routed to auto-throttle). |
| `--enable-merkle-anchor` | — | off | Publish a daily fleet Merkle root for audit tamper-evidence. Uses an in-memory anchor backend in dev. |
| `--enable-omnibus` | — | off | Run the platform omnibus wallet (requires a real chain via --rpc-url + --wallet-key). |
| `--allow-manual-credit` | — | off | Enable POST /platform/omnibus/credit, which mints user credit not tied to a verified deposit. Off by default — a faucet / testnet affordance. Leave disabled for real-money production. |
| `--kyc-provider` | — | `none` | One of: `none`, `stub`, `persona`, `onfido`. KYC provider for fund/withdraw gating (default: none). |
| `--kyc-persona-api-key` | `DARWIN_KYC_PERSONA_API_KEY` | — | Persona API key (when --kyc-provider persona). Env: DARWIN_KYC_PERSONA_API_KEY |
| `--kyc-persona-template-id` | `DARWIN_KYC_PERSONA_TEMPLATE_ID` | — | Persona inquiry template id (when --kyc-provider persona). Env: DARWIN_KYC_PERSONA_TEMPLATE_ID |
| `--kyc-persona-webhook-secret` | `DARWIN_KYC_PERSONA_WEBHOOK_SECRET` | — | Persona webhook signing secret — verifies POST /webhooks/kyc callbacks (Persona-Signature). Env: DARWIN_KYC_PERSONA_WEBHOOK_SECRET |
| `--kyc-onfido-api-token` | `ONFIDO_API_TOKEN` | — | Onfido API token (when --kyc-provider onfido). Env: ONFIDO_API_TOKEN |
| `--kyc-onfido-workflow-id` | `ONFIDO_WORKFLOW_ID` | — | Onfido Studio workflow id (when --kyc-provider onfido). Env: ONFIDO_WORKFLOW_ID |
| `--kyc-onfido-webhook-token` | `ONFIDO_WEBHOOK_TOKEN` | — | Onfido webhook token — verifies POST /webhooks/kyc callbacks (X-SHA2-Signature). Env: ONFIDO_WEBHOOK_TOKEN |
| `--kyc-onfido-region` | — | `eu` | One of: `eu`, `us`, `ca`. Onfido data region (default: eu). |
| `--twilio-account-sid` | `TWILIO_ACCOUNT_SID` | — | Twilio account SID — enables the SMS 2FA inbox (doc 15). Env: TWILIO_ACCOUNT_SID |
| `--twilio-auth-token` | `TWILIO_AUTH_TOKEN` | — | Twilio auth token (paired with --twilio-account-sid). Env: TWILIO_AUTH_TOKEN. Never log this value. |
| `--twilio-phone-number` | `TWILIO_PHONE_NUMBER` | — | Default platform Twilio number that 2FA texts land on. Env: TWILIO_PHONE_NUMBER |
| `--fcm-project-id` | `DARWIN_FCM_PROJECT_ID` | — | Firebase project id — enables FCM push for escalation tickets. Env: DARWIN_FCM_PROJECT_ID |
| `--push-provider` | — | `none` | One of: `none`, `webpush`. Mobile escalation companion push delivery (doc 22). The PWA path uses W3C Web Push + VAPID; this flag opts in. Default: none (escalations notify via the in-memory log sink only). |
| `--vapid-public-key` | `DARWIN_VAPID_PUBLIC_KEY` | — | Base64url-encoded VAPID public key (P-256). The PWA reads this from /me/push-vapid-public-key. Env: DARWIN_VAPID_PUBLIC_KEY |
| `--vapid-private-key` | `DARWIN_VAPID_PRIVATE_KEY` | — | VAPID private key — either a PEM-encoded EC private key string or a file path. Env: DARWIN_VAPID_PRIVATE_KEY |
| `--vapid-subject` | `DARWIN_VAPID_SUBJECT` | — | VAPID subject (mailto: URL or https:// site URL) per RFC 8292. Required when --push-provider webpush is set. Env: DARWIN_VAPID_SUBJECT |
| `--fiat-provider` | — | `none` | One of: `none`, `stub`, `stripe`, `moonpay`. Fiat on/off-ramp for deposits + withdrawals (doc 17). Default: none. |
| `--stripe-api-key` | `STRIPE_API_KEY` | — | Stripe secret key (when --fiat-provider stripe). Env: STRIPE_API_KEY |
| `--stripe-webhook-secret` | `STRIPE_WEBHOOK_SECRET` | — | Stripe webhook signing secret; enables POST /webhooks/stripe to verify + auto-confirm deposits. Env: STRIPE_WEBHOOK_SECRET |
| `--moonpay-api-key` | `MOONPAY_API_KEY` | — | MoonPay publishable API key (pk_) for the buy/sell widget + quote endpoint (when --fiat-provider moonpay). Env: MOONPAY_API_KEY |
| `--moonpay-secret-key` | `MOONPAY_SECRET_KEY` | — | MoonPay secret key (sk_) used to sign widget URLs + authenticate transaction lookups. Env: MOONPAY_SECRET_KEY |
| `--moonpay-webhook-key` | `MOONPAY_WEBHOOK_KEY` | — | MoonPay webhook key used to verify the Moonpay-Signature-V2 header on POST /webhooks/moonpay. Env: MOONPAY_WEBHOOK_KEY |
| `--moonpay-omnibus-address` | `MOONPAY_OMNIBUS_ADDRESS` | — | On-chain address where MoonPay-bought USDC lands (the platform omnibus deposit address). Env: MOONPAY_OMNIBUS_ADDRESS |
| `--episodic-memory` | — | `deterministic` | One of: `deterministic`, `lancedb`. Per-Darwin episodic memory substrate (doc 03). 'deterministic' keeps the in-process EpisodicMemory the genome ships with (sim + cross-engine parity). 'lancedb' installs a LanceDbEpisodicMemory per Darwin under --lancedb-path with embeddings via the LiteLLM tier-embedding model. Default: deterministic. |
| `--lancedb-path` | `DARWIN_LANCEDB_PATH` | — | Base directory for per-Darwin LanceDB tables (one subdir per darwin_id) when --episodic-memory lancedb. Env: DARWIN_LANCEDB_PATH |
| `--lancedb-embedding-model` | — | — | LiteLLM model for episodic-memory embeddings (when --episodic-memory lancedb). Default: openai/text-embedding-3-small. |
| `--maintenance-interval` | — | `86400.0` | Wall-clock gap between drive-loop maintenance runs (drift profile, Merkle anchor, omnibus reconciliation). Default 86400 (daily); set low for a soak. |

### `serve` — K8s driver (doc 08 production posture)

Per-Darwin namespace + orchestrator microVM. Requires the [k8s] extra. With --driver=in-process (default) every Darwin is an asyncio.Task in this supervisor pod; with --driver=k8s the supervisor provisions per-Darwin namespaces and drives lifecycle over HTTP RPC to the orchestrator pods.

| Flag | Env var | Default | Description |
| ---- | ------- | ------- | ----------- |
| `--driver` | — | `in-process` | One of: `in-process`, `k8s`. Per-Darwin runtime location. Default: in-process. |
| `--k8s-namespace-prefix` | — | `darwin-` | Per-Darwin namespace prefix (default: 'darwin-'). |
| `--pod-reproduction` | — | off | Let pod Darwins reproduce: the supervisor verifies the parent's DNA, signs the child, funds it on-chain from the parent's account (at most one child per parent per day) and spawns it for the same owner. Off by default. |
| `--k8s-runtime-class` | — | `kata` | RuntimeClass for the orchestrator pod (default: 'kata'). |
| `--k8s-orchestrator-image` | — | — | Image:tag for the orchestrator pod. Default reuses the supervisor image (the entrypoint differs). |
| `--k8s-orchestrator-memory` | — | `2Gi` | Memory limit for the orchestrator pod (default: 2Gi). The orchestrator OOM-kills below ~2Gi while loading the genome + adapter SDKs at boot. |
| `--k8s-egress-proxy-namespace` | — | `platform-data` | Namespace hosting the egress proxy (NetworkPolicy peer for per-Darwin orchestrator pods). |
| `--k8s-data-storage-size` | — | `1Gi` | PVC size for each pod's durable /data (audit log, checkpoint, wallet feed). Default: 1Gi. |
| `--k8s-data-storage-class` | — | — | StorageClass for the /data PVC. Empty → cluster default. |
| `--k8s-soma-storage-size` | — | `1Gi` | PVC size for the per-Darwin soma tree, persisting promoted self-mods across pod restarts (default: 1Gi). Empty string → ephemeral emptyDir (for stacks without a storage provisioner). |
| `--k8s-soma-storage-class` | — | — | StorageClass for the soma PVC. Empty → cluster default. |
| `--k8s-postgres-namespace` | — | — | Postgres namespace, for NetworkPolicy egress from orchestrator pods. Empty omits the rule (SQLite mode). |
| `--k8s-postgres-pod-label` | — | — | Postgres pod-label selector (e.g. 'app.kubernetes.io/name=postgres'). |
| `--k8s-postgres-port` | — | `5432` | Postgres port (default: 5432). |
| `--k8s-database-url-secret` | — | — | Secret reference for the orchestrator's DATABASE_URL env. Format: 'secret-name' or 'secret-name:key' (default key: DATABASE_URL). |

<!-- END GENERATED: serve flags -->

## Platform-internal environment

Set by the platform itself — by the supervisor in each orchestrator pod's manifest, or by Kubernetes — rather than by an operator. Listed so every variable the code reads is accounted for.

| Variable | Set by | Read by | Purpose |
| -------- | ------ | ------- | ------- |
| `POD_NAMESPACE` | Kubernetes downward API | supervisor | Namespace the supervisor runs in; NetworkPolicy peer for per-Darwin namespaces (defaults to `default`). |
| `DARWIN_VAULT_ROLE_ID` / `DARWIN_VAULT_SECRET_ID` | per-Darwin control Secret | orchestrator pod | The pod's own Vault AppRole credentials (never the fleet token). Also the env fallbacks for `--vault-role-id` / `--vault-secret-id`. |
| `DARWIN_PLATFORM_MONEY_RAILS` | supervisor manifest | orchestrator pod | JSON money-capabilities manifest (fiat, KYC, sanctions rails) forwarded to the pod's planner. |
| `BROWSERBASE_LIVE_VIEW_URL` | operator / pod substrate | supervisor, pod | Live-view URL surfaced to the owner on a captcha wall (`--browserbase-live-view-url`). |

## Payment HTTP endpoints (doc 17)

These routes are added by `build_app` whenever the supervisor is constructed; no separate flag enables them. Pre-approval and operator freelancer routes are `platform_operator` scoped; `/me/*` routes are owner self-serve.

### Pre-approval rules

| Method | Path | Body / Notes |
| ------ | ---- | ------------ |
| `PUT` | `/platform/wallets/{darwin_id}/preapprovals/{rule_id}` | `{max_amount_usdc, counterparty_category?, counterparty_substring?, expires_at?, max_uses?}`. Create or replace. Replacing resets `used_count` to 0. |
| `DELETE` | `/platform/wallets/{darwin_id}/preapprovals/{rule_id}` | Idempotent revoke. |
| `GET` | `/platform/wallets/{darwin_id}/preapprovals` | List active rules. |

Rules persist in the `darwin_preapprovals` SQL table (schema v6) and are inherited parent → child on reproduction. The genome's `payments.facilitator` calls `wallet.consume_preapproval(rule_id)` after a successful pre-approved payment, so `used_count` advances and writes through to persistence.

### Freelancer registry

Verified fiat-payout destinations, owner-scoped — the KYC gate at the fiat edge. The registry is backed by persistence: without `--db` every fiat payout is refused.

Operator routes (`platform_operator`):

| Method | Path | Body / Notes |
| ------ | ---- | ------------ |
| `POST` | `/platform/freelancers` | `{owner_subject, freelancer_id, fiat_destination, display_name?, kyc_status?, metadata?}`. Create or replace. |
| `DELETE` | `/platform/freelancers/{owner_subject}/{freelancer_id}` | Idempotent revoke. |
| `GET` | `/platform/freelancers/{owner_subject}` | List entries for one owner. |

Owner self-serve routes (`owner`). The owner's identity is the authenticated subject; any `owner_subject` in the body is overridden, so an owner cannot reach another user's destinations.

| Method | Path | Body / Notes |
| ------ | ---- | ------------ |
| `POST` | `/me/freelancers` | Same body as the operator route; `owner_subject` ignored. |
| `DELETE` | `/me/freelancers/{freelancer_id}` | Idempotent revoke of the caller's own registration. |
| `GET` | `/me/freelancers` | List the caller's own entries. |

Entries persist in the `freelancer_registry` SQL table (schema v5).

### Darwin → fiat payout (and refund)

| Method | Path | Body / Notes |
| ------ | ---- | ------------ |
| `POST` | `/darwins/{darwin_id}/fiat_payment` | `{amount_usdc, freelancer_id, escalation_id?}`. Two legs: wallet → omnibus address (chain), omnibus → fiat ramp. Refused when `freelancer_id` isn't registered for the Darwin's owner. |
| `POST` | `/darwins/{darwin_id}/fiat_payment/refund` | `{amount_usdc, reason?}`. Recovery path: omnibus → Darwin wallet, undoing a failed leg 2. |
| `POST` | `/darwins/{darwin_id}/omnibus/fund` | `{amount_usdc}`. Operator retry of a spawn-time `fund_child` the audit logged as `omnibus_fund_skipped`. Debits user credits and submits the chain transfer once the user has enough credit. |

### Payout address and harvest

| Method | Path | Body / Notes |
| ------ | ---- | ------------ |
| `PUT` | `/darwins/{darwin_id}/payout-address` | `{address}` — a checksummed USDC address (e.g. an exchange deposit address), sanctions-screened. In production (`--env production`) harvests and the closing sweep may only go here, and only 24 h after it was last set. |
| `POST` | `/darwins/{darwin_id}/harvest` | `{amount_usdc, destination}`. The fee applies only to the profit part of the draw; KYC-gated on the owner. Pod Darwins: the platform signs the transfer. |

### x402 storefront (public)

Enabled by `--store-price-usdc`; every Darwin sells a research report at that price, paid straight to its own wallet. No account needed — the payment is the authorization.

| Method | Path | Body / Notes |
| ------ | ---- | ------------ |
| `GET` | `/store/{darwin_id}` | The offer: service, `price_usdc`, and the x402 v2 `PaymentRequired`. |
| `POST` | `/store/{darwin_id}/report` | `{topic}`. Without `PAYMENT-SIGNATURE`: `402` + `PAYMENT-REQUIRED`. With it: the facilitator verifies, the Darwin writes the report, the payment settles, `200` + `PAYMENT-RESPONSE`. A report that can't be produced returns `503` and is never charged; at most 3 open orders per Darwin (`429`). |

## Genome hyperparameters (Layer 0 constants)

These live in `src/darwin_dna/hyperparameters.py` and are **not** CLI-configurable — they are part of the immutable DNA.

### Hard caps (inviolable)

| Parameter | Value | Enforced by |
| --------- | ----- | ----------- |
| `per_action_usdc` | $50 | Treasury policy engine + wallet-level policy |
| `per_day_usdc` | $500 | Treasury policy engine |
| `per_lifetime_usdc` | $10,000 | Treasury policy engine |

### Hyperparameters (tunable by the soma within bounds)

| Parameter | Default | Range / constraint |
| --------- | ------- | ------------------ |
| `heartbeat_interval_seconds` | 30 | [`heartbeat_min_interval_seconds` = 5, `heartbeat_max_interval_seconds` = 600] |
| `min_dispatches_before_mutation` | 100 | ≥ 1 |
| `min_recurring_failures_for_mutation` | 3 | ≥ 1 |
| `path_c_enabled` | `false` | Path-C neuron proposals are off by default (doc 21) |
| `path_c_require_human_review` | `true` | A Path-C proposal waits for an operator |
| `min_profit_for_reproduction_usdc` | $25 | ≥ 0 |
| `profit_transfer_fraction` | 0.5 | (0, 1) |
| `reproduction_fitness_window_days` | 7 | ≥ 1 |
| `generation_soft_cap` | 20 | ≥ 2 |
| `fitness_profit_weight` | 1.0 | Fixed (DNA) |
| `fitness_diversity_weight` | 0.1 | Fixed (DNA) |
| `fitness_escalation_quality_weight` | 0.05 | Fixed (DNA) |
| `fitness_policy_violation_penalty` | 10.0 | Fixed (DNA) |
| `tier_coding_max_daily_calls` | 20 | ≥ 1 |
| `tier_reasoning_max_daily_calls` | 2,000 | ≥ 1 |
| `tier_fast_max_daily_calls` | 50,000 | ≥ 1 |
| `minimum_viable_balance_usdc` | $1 | ≥ 0 |
| `revenue_grace_window_seconds` | 86,400 (24 h) | ≥ 0 |
| `ledger_reconciliation_tolerance_usdc` | $0.01 | Max ledger ↔ wallet drift before an alert |
| `archive_max_entries` | 256 | Mutation-archive bound |
| `episodic_max_entries` | 512 | Episodic-memory bound |
| `semantic_max_staged` | 64 | Staged semantic lessons bound |
| `semantic_min_evidence_for_promotion` | 3 | Evidence needed to promote a lesson |

## Monitor quotas (platform-side)

Code constants in `src/darwin_platform/observability/monitoring.py`.

| Quota | WARN | CRITICAL |
| ----- | ---- | -------- |
| `spend_velocity_usd_per_hour` | $100 | $300 |
| `failure_rate` (lifetime) | 30% | 60% |
| `heartbeat_gap_seconds` | 1,200 | 3,000 |
| `escalation_rate_per_hour` | 5 | 20 |
| `spread_spend_count_per_hour` | 30 (below $1 floor) | 80 |
| `dedup_window_seconds` | 60 | — |

## Docker Compose environment

The repo-root `.env` (from [`.env.example`](../../.env.example), loaded via `--env-file .env` by `make docker-up` — there is no `infra/docker/.env`) configures the Compose stack. `.env.example` is the annotated source of truth for every variable; the main groups are:

| Variables | Service | Purpose |
| --------- | ------- | ------- |
| LLM provider keys (e.g. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) | litellm | Real model access through the proxy |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | postgres, supervisor | Compose Postgres credentials (dev-only defaults) |
| `E2B_API_KEY`, `BROWSERBASE_URL`, `BROWSERBASE_API_KEY` | supervisor | Real sandbox / browser neurons; empty values degrade to fakes |
| `DARWIN_RPC_URL`, `DARWIN_WALLET_KEY` | supervisor | Optional real testnet wallet |

See [`infra/docker/README.md`](../../infra/docker/README.md) for the full Compose setup.

## Helm values (summary)

The full values reference is in [`infra/charts/darwin-platform/README.md`](../../infra/charts/darwin-platform/README.md). Key values:

| Value path | Maps to | Notes |
| ---------- | ------- | ----- |
| `database.url` / `database.existingSecret` | `--db` | Postgres DSN or Secret reference |
| `leaderElection.enabled` | `--leader-election` | Requires Postgres |
| `auth.enabled` / `auth.existingSecret` | `--auth-tokens` | Secret holding `tokens.json` |
| `tracing.endpoint` | OTEL env vars | Injected as container env |
| `serviceMonitor.enabled` | Prometheus scrape | Requires prometheus-operator |
| `networkPolicy.enabled` | Kubernetes NetworkPolicy | Default-deny egress + selective allow |
| `podDisruptionBudget.enabled` | PodDisruptionBudget | HA supervisor (with `replicaCount ≥ 2`) |

## Precedence rules

1. CLI flag (highest priority).
2. Environment variable, where documented as the flag's fallback.
3. Code default (lowest; always safe for development).

## What happens when nothing is configured

With zero flags and zero env vars, `serve` starts with:

- In-memory state (no persistence — lost on restart).
- `FakeWallet`, `FakeVault`, `FakeBrowser`, `FakeSandbox`, `FakeLLM`.
- No auth (trust every caller).
- No KYC gate, no sanctions provider beyond the empty local denylist.
- No notifications (alerts go to structlog only).
- The in-process driver (Darwins are asyncio tasks).

This is the test/CI mode: deterministic, zero external dependencies, zero secrets.
