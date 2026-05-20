# Darwin platform — operator runbook

The supervisor + every adapter is built and reachable via Supervisor
ctor / CLI / HTTP / Helm values. This runbook is the deployer's
checklist: which env vars to set per provider, when to rotate, how to
respond to common failures, and where to look first when something
trips.

For an architecture tour, see `docs/engineering/`. For the chart's
values reference, see `deploy/helm/darwin-platform/README.md`.

---

## 1. Provider enablement matrix

Every adapter is opt-in. The supervisor's `_build_*` helpers in
`src/darwin_platform/__main__.py` consume env vars; the same vars are
projected into the Helm Deployment via `providers.*` values.

| Provider | Doc | Required env vars | Helm path |
|---|---|---|---|
| **EVM wallet** (Base USDC) | 17 | `DARWIN_RPC_URL`, `DARWIN_WALLET_KEY` | `extraEnv` |
| **Solana wallet** | 17 | `DARWIN_SOLANA_RPC_URL`, `DARWIN_SOLANA_WALLET_KEY` + `--chain-networks=base,solana` | `extraEnv` |
| **Stellar wallet** | 17 | `DARWIN_STELLAR_HORIZON_URL`, `DARWIN_STELLAR_SECRET` + `--chain-networks=...,stellar` | `extraEnv` |
| **HashiCorp Vault** | 16 | `DARWIN_VAULT_URL`, `DARWIN_VAULT_TOKEN` | `extraEnv` |
| **Browserbase** | 15 | `BROWSERBASE_URL`, `BROWSERBASE_API_KEY` | `extraEnv` |
| **E2B sandbox** | 14 | `E2B_API_KEY` | `extraEnv` |
| **Stripe** (fiat) | 17 | `STRIPE_API_KEY` | `providers.fiat.stripeApiKey` |
| **MoonPay** (fiat) | 17 | `MOONPAY_API_KEY` | `providers.fiat.moonpayApiKey` |
| **Chainalysis** | 17 | `--sanctions-provider chainalysis` + `DARWIN_SANCTIONS_API_KEY` | `providers.sanctions.{provider,apiKey}` |
| **TRM Labs** | 17 | `--sanctions-provider trm` + `DARWIN_SANCTIONS_API_KEY` | `providers.sanctions.{provider,apiKey}` |
| **Slack** (notifications) | 08 | `DARWIN_SLACK_WEBHOOK_URL` | `providers.notifications.slackWebhookUrl` |
| **PagerDuty** (alerts) | 08 | `DARWIN_PAGERDUTY_ROUTING_KEY` | `providers.notifications.pagerdutyRoutingKey` |
| **OTel** (tracing) | 08 | `OTEL_EXPORTER_OTLP_ENDPOINT` | `tracing.endpoint` |
| **Postgres persistence** | 08 | `DATABASE_URL` (postgres://...) | `database.url` / `database.existingSecret` |

### Setting them all via `existingSecret` (recommended)

Each `providers.*` group accepts an `existingSecret` reference. The
chart projects each known key via `secretKeyRef` so secrets never
land in `values.yaml`. Use ExternalSecrets or SealedSecrets to
populate the Secrets out-of-band.

```yaml
# values-prod.yaml
providers:
  sanctions:
    provider: "chainalysis"
    existingSecret: "darwin-sanctions"  # contains key: sanctions-api-key
  notifications:
    existingSecret: "darwin-notifications"  # keys: darwin_slack_webhook_url, etc.
  fiat:
    existingSecret: "darwin-fiat"  # keys: stripe-api-key, moonpay-api-key
```

---

## 2. Key-rotation cadence

| Key | Cadence | Procedure |
|---|---|---|
| **Platform signing key** | 90d (suspected compromise → immediate) | `POST /platform/signing/rotate` (operator scope). Drains old key over the chart's default 90-day window. See `key_rotation.py`. |
| **Wallet private key** | Per-Darwin lifecycle (treat as ephemeral) | Spawn replacement Darwin; let the old one drain via harvest. No in-place key rotation — the wallet IS the Darwin's identity. |
| **Stripe API key** | 12 months | Rotate in Stripe dashboard → update Secret → restart Deployment. No code change. |
| **Chainalysis / TRM API key** | Per provider's own policy (usually 12 months) | Same as Stripe — Secret update + pod restart. |
| **Vault root token** | NEVER use root in prod; AppRole + auto-revoke | The HvacVaultClient receives a token at startup. Use Vault Agent / k8s auth so the token is auto-renewed. |
| **Auth bearer tokens** | Per ExternalSecrets policy | The chart re-reads the mounted file on pod restart; rotate by updating the Secret. |

### Rotating the platform signing key

```bash
# Operator scope — needs auth.
curl -XPOST -H "Authorization: Bearer $OPS_TOKEN" \
  https://darwin.example.com/platform/signing/rotate \
  -d '{}'   # optional: {"drain_window_seconds": 86400} for emergency rotation
```

The previous key stays accepted for `drain_window_seconds` (default
90 days) so existing manifests verify until they re-sign at the next
reproduction. Verify the public key set via:

```bash
curl https://darwin.example.com/platform/signing/public_key
```

---

## 3. Monitoring playbook

### Critical alerts (page immediately)

| Alert (PromQL) | Likely cause | First action |
|---|---|---|
| `darwin_platform_sanctions_refusals_total` increasing | Sanctioned address attempted | Pull `audit_log` entries for `wallet_error` w/ `sanctions:` prefix; investigate provenance of the recipient. |
| `darwin_platform_darwins{state="killed"}` jump | Mass kill | Check `audit_log` for `kill` actions; possible operator-initiated mass quarantine OR critical-detector cascade. |
| `darwin_platform_alerts_total{severity="critical"}` rate > 0 | Critical-level detector | `GET /platform/alerts?min_severity=critical` for the active set. |
| `darwin_platform_throttle_installed{kind="egress"}` > 0 | Auto-throttle fired on spread-spend | Inspect the affected Darwin's recent wallet movements via `GET /darwins/{id}/billing`. |

### Warn-level (review same business day)

| Alert | Action |
|---|---|
| Heartbeat gap > 1200s | Drive loop stuck; SSH the pod and inspect `runtime.state.tick` advancing. |
| Failure rate > 30% | Get `GET /darwins/{id}/events?kind=outcome:fail` and look for a single error_code dominating. |
| Browser cache promotion stalled | The cache may have demoted; check `GET /platform/browser/stats`. |

### Where to look first

| Question | Endpoint |
|---|---|
| Who did what + when? | `GET /platform/audit` (operator) |
| Why was a payment refused? | `GET /darwins/{id}/billing` + the audit log's `wallet_error` entries |
| Is leadership healthy? | `GET /healthz` + `/readyz` |
| Latest persistence sweep? | `audit_log` action=`vault_retention_purge` |
| Sibling Darwin's lessons? | `GET /darwins/{id}/proposals` + the audit log's `cross_pollination_*` actions |

---

## 4. Common failure modes

### `KeyRotationError: persistence required`
Rotation needs the `--db` flag. Postgres or SQLite both work; the
in-memory mode (no `--db`) refuses because nothing would persist the
retired key.

### `FiatRampError: not configured`
The supervisor didn't get a `fiat_ramp=...` kwarg. Wire one via
`providers.fiat.stripeApiKey` in Helm OR construct the supervisor in
code with `Supervisor(fiat_ramp=StripeFiatRamp(stripe_client=stripe))`.

### `ChainRoutingError: no chain accepted ...`
No configured chain has enough USDC liquidity for the requested
amount AND a matching recipient address. Add liquidity to one chain
OR add the chain whose address shape matches the recipient (e.g. a
`G...` address needs `--chain-networks=...,stellar`).

### `NeuronProposalError: forbidden tags for Path-C: ['treasury.write']`
A Darwin tried to author a neuron with a privileged tag.
**Expected behaviour** — the gate is doing its job. The audit log
records the refusal under `neuron_proposal.refused`.

### `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread`
Should NEVER happen in production (we set `check_same_thread=False`).
If it does, the SQLite library has been patched in a way that
overrides our flag. Switch to the Postgres backend.

### `helm install` rendering fails with provider templates
Run `helm template darwin deploy/helm/darwin-platform > /tmp/out.yaml`
and grep for the failing kind. The CI's `helm template (provider
integrations)` step covers the supported combinations; an
unsupported combo (e.g. `existingSecret` AND inline values for the
SAME group) is undefined — pick one or the other.

---

## 5. Backup + disaster recovery

### Backup

- **Postgres**: standard Postgres backup. The `audit_entries` table
  is the most important; it's hash-chained, so a corrupt restore is
  detectable (`AuditLogger.verify()` walks the chain on boot).
- **SQLite**: snapshot the `/data/state.db` file at the chart's PVC.
  WAL mode is on; use `sqlite3 state.db .backup` for a consistent
  snapshot.
- **Vault**: HashiCorp Vault's own snapshot mechanism.
  Operator-side; the Darwin platform doesn't manage Vault backups.
- **On-chain wallets**: chain itself is the backup. Hot-wallet
  private keys should be in a KMS/HSM, not the persistence DB
  (treat `platform_signing_key` as the ONE exception — it's the
  manifest trust root).

### Restore

1. Restore the persistence DB (Postgres dump or SQLite file).
2. Start the supervisor against it. `AuditLogger.verify()` is called
   at every Darwin's boot; a chain mismatch refuses to start the
   Darwin — the operator can then either re-sign from the audit log
   tip OR mark the Darwin killed.
3. Confirm via `GET /platform/audit?limit=1` that the chain tip
   matches the pre-restart tip.
4. Confirm via `GET /healthz` that `persistence: "sqlite"` or
   `"postgres"` matches expectation.

### A Darwin's audit chain reports tamper

The platform refuses to drive that Darwin until the operator
intervenes. Options:

- **Restore from snapshot**: take the most recent good snapshot and
  re-open. Acceptable downtime depends on the chain backup cadence.
- **Mark killed + accept the lossy split**: the lifecycle row's
  `killed=1` halts the drive loop; the audit chain stays intact
  up to the tamper point.

---

## 6. Pen-test + red-team cadence

Doc 18 commits to a red-team review of every Layer 0 release. The
automated `test_redteam.py` suite (run on every CI build) covers the
hot paths. A *manual* red-team pass should happen:

- Pre-release for any change touching `darwin_dna/manifest.py`,
  `audit_logger.py`, `kill_switch.py`, or `treasury_policy.py`.
- Annually for the whole platform, regardless of changes.
- After any incident touching the audit chain, signing key, or
  vault tier model.

Use the `test_redteam.py` scenarios as the starting checklist.

---

## 7. Cost dashboards

Grafana dashboard JSON: `deploy/grafana/darwin-platform-dashboard.json`.

Panels relevant for cost:
- Platform revenue (USDC, cumulative)
- Harvest fees (USDC, cumulative)
- Browser cost (USDC)
- Throttle usage today (by resource kind)

Alert: configure a Grafana / Prometheus alert on browser cost growth
rate to catch a runaway browser session before harvest fees pay it
back.
