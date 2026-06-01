# darwin-platform Helm chart

Deploys the Darwin platform supervisor (docs/engineering/08) on
Kubernetes. One Deployment, one Service, a PVC for SQLite persistence,
optional Secret for bearer tokens, optional Ingress, optional
ServiceMonitor for kube-prometheus-stack, and a NetworkPolicy that
defaults to deny-egress with explicit allow rules.

## Quickstart

```bash
# Render and inspect before installing.
helm template darwin infra/charts/darwin-platform | less

# Install with defaults (no auth, no ingress — DEV ONLY).
helm install darwin infra/charts/darwin-platform

# Install with production-flavoured overrides.
helm install darwin infra/charts/darwin-platform \
  --namespace darwin --create-namespace \
  --set image.tag=sha-abc1234 \
  --set auth.enabled=true \
  --set auth.existingSecret=darwin-platform-tokens \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set serviceMonitor.enabled=true \
  --set tracing.endpoint=http://otel-collector.observability:4318
```

The chart deploys **only the supervisor**. LiteLLM, Postgres,
Prometheus, and Jaeger are typically managed by sibling charts in the
same cluster:

- LiteLLM: [berriai/litellm](https://github.com/BerriAI/litellm/tree/main/deploy/charts/litellm-helm)
- Postgres: [bitnami/postgresql](https://github.com/bitnami/charts/tree/main/bitnami/postgresql)
- Prometheus + Jaeger: kube-prometheus-stack + jaegertracing/jaeger-operator
- ExternalSecrets / SealedSecrets: same cluster, your choice

## Values reference

| Key                                          | Default                                              | Notes                                                                                                      |
| -------------------------------------------- | ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `image.repository`                           | `ghcr.io/quantium-rock/darwin-agent/darwin-platform` | Image registry path.                                                                                       |
| `image.tag`                                  | `""` → falls back to `Chart.appVersion`              | **Pin sha-XXXXXX in prod.**                                                                                |
| `image.pullPolicy`                           | `IfNotPresent`                                       |                                                                                                            |
| `image.pullSecrets`                          | `[]`                                                 | Add `imagePullSecrets` entries for private registries.                                                     |
| `replicaCount`                               | `1`                                                  | Raise only together with `leaderElection.enabled` + Postgres.                                              |
| `resources`                                  | requests 100m / 256Mi, limits 1000m / 1Gi            | Tune for your fleet size.                                                                                  |
| `podSecurityContext`                         | `fsGroup: 1000, runAsNonRoot: true, runAsUser: 1000` | Image ships a non-root `darwin` user (UID 1000).                                                           |
| `containerSecurityContext`                   | `allowPrivilegeEscalation: false, drop: ["ALL"]`     | `readOnlyRootFilesystem` is off by default (uvicorn writes cache dirs at startup).                         |
| `persistence.enabled`                        | `true`                                               | PVC mounted at `/data` for SQLite + audit chain.                                                           |
| `persistence.size`                           | `10Gi`                                               | Size the PVC.                                                                                              |
| `persistence.storageClass`                   | `""`                                                 | Empty = cluster default.                                                                                   |
| `persistence.existingClaim`                  | `""`                                                 | Point at a pre-created PVC if you manage storage out-of-band.                                              |
| `service.type` / `port`                      | `ClusterIP` / `8000`                                 | Expose via Ingress, not LoadBalancer.                                                                      |
| `ingress.enabled`                            | `false`                                              | Off by default — auth must be on before exposing.                                                          |
| `ingress.className` / `hosts` / `tls`        | empty                                                | Standard Ingress fields.                                                                                   |
| `auth.enabled`                               | `false`                                              | Off = trust-everyone (DEV ONLY).                                                                           |
| `auth.tokens`                                | `[]`                                                 | In-line tokens; ends up in a Secret rendered by the chart. Avoid in production.                            |
| `auth.existingSecret` / `existingSecretKey`  | `""` / `tokens.json`                                 | Reference a Secret you manage out-of-band (ExternalSecrets/SealedSecrets).                                 |
| `database.url`                               | `""`                                                 | postgresql:// URL — switches to Postgres mode and drops the PVC.                                           |
| `database.existingSecret`                    | `""`                                                 | Secret holding `DATABASE_URL`; preferred over inline `database.url` in prod.                               |
| `database.existingSecretKey`                 | `DATABASE_URL`                                       | Key within the existing Secret.                                                                            |
| `leaderElection.enabled`                     | `false`                                              | Enable HA via Postgres advisory locks. Requires `database.*` to be set. See [HA](#sqlite-vs-postgres--ha). |
| `leaderElection.lockKey`                     | `""` → supervisor default                            | Override the advisory-lock BIGINT key to isolate multiple platforms on one Postgres.                       |
| `tracing.endpoint`                           | `""`                                                 | OTLP collector. Empty disables tracing env injection.                                                      |
| `tracing.serviceName` / `protocol`           | `darwin-supervisor` / `http/protobuf`                | Standard OTel.                                                                                             |
| `serviceMonitor.enabled`                     | `false`                                              | On if you have prometheus-operator.                                                                        |
| `serviceMonitor.interval` / `scrapeTimeout`  | `15s` / `10s`                                        |                                                                                                            |
| `serviceMonitor.labels`                      | `{}`                                                 | Selector labels your Prometheus picks up (e.g. `release: prometheus`).                                     |
| `serviceMonitor.namespace`                   | `""` → Release.Namespace                             |                                                                                                            |
| `networkPolicy.enabled`                      | `true`                                               | Default-deny egress + selective allow rules.                                                               |
| `networkPolicy.ingress.from`                 | `darwin.io/api-client=true` podSelector              | Tighten in production to specific namespaces.                                                              |
| `networkPolicy.egress.litellm.*`             | `namespace=""`, `port=4000`                          | Allow egress to the LiteLLM proxy. Required if you use it.                                                 |
| `networkPolicy.egress.otlp.*`                | `namespace=""`, `ports=[4317,4318]`                  | Allow egress to the OTLP collector. Required if `tracing.endpoint` is set.                                 |
| `networkPolicy.egress.postgres.*`            | `namespace=""`, `port=5432`                          | Allow egress to Postgres when using `database.*`.                                                          |
| `networkPolicy.egress.extraCIDRs`            | `[]`                                                 | Public destinations (e.g. `0.0.0.0/0:443` for chain RPCs, Vault, Browserbase).                            |
| `serviceAccount.create`                      | `true`                                               | Set `false` to bring your own.                                                                             |
| `serviceAccount.name`                        | `""`                                                 | Auto-generated when empty.                                                                                 |
| `serviceAccount.annotations`                 | `{}`                                                 | IRSA / Workload Identity annotations.                                                                      |
| `podDisruptionBudget.enabled`                | `false`                                              | Enable only with `replicaCount > 1` + HA (a PDB with minAvailable=1 + replicas=1 blocks voluntary drains). |
| `nodeSelector`                               | `{}`                                                 |                                                                                                            |
| `tolerations`                                | `[]`                                                 |                                                                                                            |
| `affinity`                                   | `{}`                                                 |                                                                                                            |
| `podAnnotations`                             | `{}`                                                 |                                                                                                            |
| `extraEnv`                                   | `[]`                                                 | Extra env vars injected verbatim. Use to pass real-adapter secrets (see below).                            |

## SQLite vs Postgres / HA

The chart defaults to SQLite on a PVC — right for single-replica
deployments and dev clusters. Switch to Postgres when you want:

- **HA / multi-replica** — leader election uses `pg_try_advisory_lock`;
  exactly one pod drives Darwins while the others stay warm. Failover
  window is ≈ 5 s (one heartbeat).
- **Backups + point-in-time recovery** that play with your standard
  Postgres ops (rather than PVC snapshots).
- **A managed database** (RDS, Cloud SQL, Neon, Supabase) so the
  supervisor is stateless from the cluster's view.

```bash
# Inline DSN (simplest; the URL ends up in the rendered manifest).
helm install darwin infra/charts/darwin-platform \
  --set database.url='postgresql://darwin:CHANGE_ME@postgres-rw:5432/darwin'

# Production (recommended): reference a Secret managed by ExternalSecrets
# / SealedSecrets. The chart never sees the URL in plain text.
helm install darwin infra/charts/darwin-platform \
  --set database.existingSecret=darwin-db-url
```

### Running HA (multi-replica)

Requires Postgres and `leaderElection.enabled`:

```bash
helm install darwin infra/charts/darwin-platform \
  --set replicaCount=3 \
  --set leaderElection.enabled=true \
  --set database.existingSecret=darwin-db-url
```

The Deployment strategy switches to `RollingUpdate` automatically.
The readiness probe changes to `/readyz`, which returns 503 on
followers, so the Service routes API traffic only to the current
leader. The `podDisruptionBudget` should be enabled alongside:

```bash
  --set podDisruptionBudget.enabled=true
```

Switching to Postgres drops the PVC from the rendered manifests; the
supervisor reads `DATABASE_URL` from env and connects directly. Make
sure `networkPolicy.egress.postgres.namespace` + `podLabels` point at
your Postgres so the supervisor pod can reach it.

Migration between modes is not automated — backing up the SQLite
audit chain and replaying it into Postgres is out of scope; switch
modes during a maintenance window when the audit log can start fresh.

## Real provider adapters

The supervisor ships with in-memory fakes for the wallet, vault,
browser, and sandbox. In production, activate the real adapters by
injecting the corresponding environment variables via `extraEnv` or
`envFrom` (preferred for secrets):

| Env var              | CLI flag              | Purpose                                                   |
| -------------------- | --------------------- | --------------------------------------------------------- |
| `DARWIN_RPC_URL`     | `--rpc-url`           | EVM HTTP RPC endpoint (e.g. `https://sepolia.base.org`)   |
| `DARWIN_WALLET_KEY`  | `--wallet-key`        | Hex private key for on-chain USDC signing                 |
| `DARWIN_VAULT_URL`   | `--vault-url`         | HashiCorp Vault address                                   |
| `DARWIN_VAULT_TOKEN` | `--vault-token`       | Vault token with read+write on the KV mount               |
| `BROWSERBASE_URL`    | `--browserbase-url`   | Browserbase WebSocket endpoint                            |
| `BROWSERBASE_API_KEY`| `--browserbase-api-key` | Browserbase API key                                     |
| `E2B_API_KEY`        | `--e2b-api-key`       | E2B API key for the MicroVM sandbox (code-running neurons) |
| `DARWIN_SOLANA_RPC_URL` / `DARWIN_SOLANA_WALLET_KEY` | `--solana-rpc-url` / `--solana-wallet-key` | Solana fallback chain (doc 17). Enable with `--chain-networks=base,solana`. |
| `DARWIN_STELLAR_HORIZON_URL` / `DARWIN_STELLAR_SECRET` | `--stellar-horizon-url` / `--stellar-secret` | Stellar fallback chain. Enable with `--chain-networks=base,stellar`. |
| `STRIPE_API_KEY`     | (consumed by Supervisor's fiat ramp) | Stripe Connect deposits + payouts (doc 17). |
| `MOONPAY_API_KEY`    | (consumed by Supervisor's fiat ramp) | MoonPay HTTP API for international ramps. |
| `DARWIN_SANCTIONS_API_KEY` | `--sanctions-api-key` | Chainalysis / TRM screening provider key (set `--sanctions-provider chainalysis|trm`). |
| `DARWIN_SLACK_WEBHOOK_URL` | `--slack-webhook-url` | Slack escalation notifications. |
| `DARWIN_ESCALATION_WEBHOOK_URL` | `--escalation-webhook-url` | Generic JSON-POST escalation webhook. |
| `DARWIN_ALERT_WEBHOOK_URL` | `--alert-webhook-url` | Generic JSON-POST monitor alerts. |
| `DARWIN_PAGERDUTY_ROUTING_KEY` | `--pagerduty-routing-key` | PagerDuty Events v2 routing key. |

When `DARWIN_RPC_URL` + `DARWIN_WALLET_KEY` are both set, the x402
HTTP client is wired automatically — neurons with `web.operate` /
`web.publish` / `web.signup` capability tags receive a pre-funded HTTP
client that settles HTTP 402 responses against the Darwin's wallet
transparently.

### SDK extras bundled with the image

The default `darwin-platform` image installs every optional adapter
SDK (`solana`, `solders`, `stellar-sdk`, `stripe`, `pypdf`, etc.) so a
chart consumer never needs a custom build to enable a provider —
flipping the right env var or values key is enough. The slim path
(genome + supervisor only, no real adapters) is the test posture, not
the production posture.

### Example: secret-per-adapter via envFrom

```yaml
# values-prod.yaml
extraEnv: []  # keep empty — use envFrom instead to avoid values leaking

# In a separate override file, or add envFrom to the Deployment via
# a patch if not yet surfaced as a chart value:
#   envFrom:
#     - secretRef:
#         name: darwin-wallet-credentials
#         # keys: DARWIN_RPC_URL, DARWIN_WALLET_KEY
#     - secretRef:
#         name: darwin-vault-credentials
#         # keys: DARWIN_VAULT_URL, DARWIN_VAULT_TOKEN
#     - secretRef:
#         name: darwin-browserbase-credentials
#         # keys: BROWSERBASE_API_KEY
#     - secretRef:
#         name: darwin-e2b-credentials
#         # keys: E2B_API_KEY
```

Until `envFrom` is a first-class chart value, the simplest production
approach is `extraEnv` with `valueFrom.secretKeyRef`:

```bash
helm install darwin infra/charts/darwin-platform \
  --set 'extraEnv[0].name=DARWIN_RPC_URL' \
  --set 'extraEnv[0].valueFrom.secretKeyRef.name=darwin-wallet' \
  --set 'extraEnv[0].valueFrom.secretKeyRef.key=rpc_url' \
  --set 'extraEnv[1].name=DARWIN_WALLET_KEY' \
  --set 'extraEnv[1].valueFrom.secretKeyRef.name=darwin-wallet' \
  --set 'extraEnv[1].valueFrom.secretKeyRef.key=private_key'
```

The network policy's `extraCIDRs` must be opened for any public
endpoints the adapters reach (chain RPCs, Vault, Browserbase, E2B):

```yaml
networkPolicy:
  egress:
    extraCIDRs:
      - cidr: 0.0.0.0/0
        ports: [443]
```

## Production checklist

Before exposing publicly:

- [ ] `auth.enabled=true` AND `auth.existingSecret` pointing at an
      ExternalSecrets-managed Secret (do not store tokens in values.yaml).
- [ ] `image.tag` pinned to a specific `sha-` tag, NOT `latest`.
- [ ] `database.existingSecret` set if Postgres mode, or
      `persistence.storageClass` chosen explicitly + matching snapshot
      policy if SQLite mode.
- [ ] `ingress.tls` configured with cert-manager or your TLS provider.
- [ ] `serviceMonitor.enabled=true` and Prometheus + Alertmanager wired
      to the doc-08 alerts on `darwin_platform_alerts_total{severity}`.
- [ ] `networkPolicy.enabled=true` (default) and egress rules pointing
      at your actual LiteLLM + Postgres + OTLP locations; `extraCIDRs`
      opened for any real-adapter public endpoints.
- [ ] Real-adapter secrets (`DARWIN_WALLET_KEY`, `DARWIN_VAULT_TOKEN`,
      etc.) injected via `extraEnv[*].valueFrom.secretKeyRef` — never
      stored in values.yaml.
- [ ] Backups configured on the PVC's StorageClass (you'll lose the
      audit chain otherwise, which doc 08 forbids).
- [ ] If `replicaCount > 1`: `leaderElection.enabled=true`,
      `database.*` pointing at Postgres, and
      `podDisruptionBudget.enabled=true`.

## Upgrades

```bash
helm upgrade darwin infra/charts/darwin-platform -f my-values.yaml
```

The PVC has `helm.sh/resource-policy=keep`, so `helm uninstall` does
NOT delete the durable state. Wipe explicitly when you want to lose
the audit chain:

```bash
helm uninstall darwin
kubectl delete pvc darwin-darwin-platform-data
```

## Limitations

- No native `envFrom` chart value yet. Use `extraEnv[*].valueFrom.secretKeyRef`
  to reference Secrets for real-adapter credentials, or apply a Kustomize/Helm
  post-renderer patch to add `envFrom` blocks.
- Bearer tokens only. Production should layer OIDC at the Ingress; the
  supervisor accepts a pre-validated token via the same Authorization
  header.
- No HPA. Supervisor is currently CPU/memory bound by Darwin count;
  vertical scale or a fleet-shard pattern is the right answer (each
  shard owning a slice of `darwin_id`s).
