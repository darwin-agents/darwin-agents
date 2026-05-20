# darwin-platform Helm chart

Deploys the Darwin platform supervisor (docs/engineering/08) on
Kubernetes. One Deployment, one Service, a PVC for SQLite persistence,
optional Secret for bearer tokens, optional Ingress, optional
ServiceMonitor for kube-prometheus-stack, and a NetworkPolicy that
defaults to deny-egress with explicit allow rules.

## Quickstart

```bash
# Render and inspect before installing.
helm template darwin deploy/helm/darwin-platform | less

# Install with defaults (no auth, no ingress — DEV ONLY).
helm install darwin deploy/helm/darwin-platform

# Install with production-flavoured overrides.
helm install darwin deploy/helm/darwin-platform \
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

| Key                                       | Default                                                       | Notes                                                                                  |
| ----------------------------------------- | ------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `image.repository`                        | `ghcr.io/quantium-rock/darwin-agent/darwin-platform`          | Image registry path.                                                                   |
| `image.tag`                               | `""` → falls back to `Chart.appVersion`                       | **Pin sha-XXXXXX in prod.**                                                            |
| `replicaCount`                            | `1`                                                           | Single-writer per process; do not raise without the Postgres persistence port.         |
| `resources`                               | requests 100m / 256Mi, limits 1000m / 1Gi                     | Tune for your fleet size.                                                              |
| `persistence.enabled`                     | `true`                                                        | PVC mounted at `/data` for SQLite + audit chain.                                       |
| `persistence.size`                        | `10Gi`                                                        | Size the PVC.                                                                          |
| `persistence.storageClass`                | `""`                                                          | Empty = cluster default.                                                               |
| `persistence.existingClaim`               | `""`                                                          | Point at a pre-created PVC if you manage storage out-of-band.                          |
| `service.type` / `port`                   | `ClusterIP` / `8000`                                          | Expose via Ingress, not LoadBalancer.                                                  |
| `ingress.enabled`                         | `false`                                                       | Off by default — auth must be on before exposing.                                      |
| `ingress.className` / `hosts` / `tls`     | empty                                                         | Standard Ingress fields.                                                               |
| `auth.enabled`                            | `false`                                                       | Off = trust-everyone (DEV ONLY).                                                       |
| `auth.tokens`                             | `[]`                                                          | In-line tokens; ends up in a Secret rendered by the chart. Avoid in production.        |
| `auth.existingSecret` / `existingSecretKey` | `""` / `tokens.json`                                        | Reference a Secret you manage out-of-band (ExternalSecrets/SealedSecrets).             |
| `tracing.endpoint`                        | `""`                                                          | OTLP collector. Empty disables tracing env injection.                                  |
| `tracing.serviceName` / `protocol`        | `darwin-supervisor` / `http/protobuf`                         | Standard OTel.                                                                         |
| `serviceMonitor.enabled`                  | `false`                                                       | On if you have prometheus-operator.                                                    |
| `serviceMonitor.labels`                   | `{}`                                                          | Selector labels your Prometheus picks up (e.g. `release: prometheus`).                 |
| `networkPolicy.enabled`                   | `true`                                                        | Default-deny egress + selective allow rules.                                           |
| `networkPolicy.ingress.from`              | `app-client=true` podSelector                                 | Tighten in production to specific namespaces.                                          |
| `networkPolicy.egress.litellm.*`          | `namespace=""`                                                | Allow egress to the LiteLLM proxy. Required if you use it.                             |
| `networkPolicy.egress.otlp.*`             | `namespace=""`                                                | Allow egress to the OTLP collector. Required if `tracing.endpoint` is set.             |
| `networkPolicy.egress.postgres.*`         | `namespace=""`                                                | Allow egress to Postgres (future use).                                                 |
| `networkPolicy.egress.extraCIDRs`         | `[]`                                                          | Public destinations (e.g. Coinbase APIs for the real wallet adapter).                  |

## Production checklist

Before exposing publicly:

- [ ] `auth.enabled=true` AND `auth.existingSecret` pointing at an
      ExternalSecrets-managed Secret (do not store tokens in values.yaml).
- [ ] `image.tag` pinned to a specific `sha-` tag, NOT `latest`.
- [ ] `persistence.storageClass` chosen explicitly + matching snapshot
      policy.
- [ ] `ingress.tls` configured with cert-manager or your TLS provider.
- [ ] `serviceMonitor.enabled=true` and Prometheus + Alertmanager wired
      to the doc-08 alerts on `darwin_platform_alerts_total{severity}`.
- [ ] `networkPolicy.enabled=true` (default) and egress rules pointing
      at your actual LiteLLM + Postgres + OTLP locations.
- [ ] Backups configured on the PVC's StorageClass (you'll lose the
      audit chain otherwise, which doc 08 forbids).

## Upgrades

```bash
helm upgrade darwin deploy/helm/darwin-platform -f my-values.yaml
```

The PVC has `helm.sh/resource-policy=keep`, so `helm uninstall` does
NOT delete the durable state. Wipe explicitly when you want to lose
the audit chain:

```bash
helm uninstall darwin
kubectl delete pvc darwin-darwin-platform-data
```

## Limitations + roadmap

- Single replica. Multi-instance support requires the Postgres-backed
  persistence port + leader election (TODO).
- Bearer tokens only. Production should layer OIDC at the Ingress; the
  supervisor accepts a pre-validated token via the same Authorization
  header.
- No HPA. Supervisor is currently CPU/memory bound by Darwin count;
  vertical scale or a fleet-shard pattern is the right answer (each
  shard owning a slice of `darwin_id`s).
