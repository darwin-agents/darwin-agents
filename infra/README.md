# Darwin — infrastructure

Everything needed to build, run, and deploy the Darwin platform, grouped
by deployment target. Pick the subdir that matches where you're running.

| Path                         | What it is                                                                                     | Start here                                       |
| ---------------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| [`docker/`](docker/)         | **Local dev** — Docker Compose stack (supervisor + web + Postgres + Redis + LiteLLM + Prometheus + Alertmanager + OTel collector + Jaeger). No Kubernetes. | [`docker/README.md`](docker/README.md)           |
| [`charts/`](charts/)         | **Packaging** — the `darwin-platform` Helm chart. The single source of truth for the Kubernetes workload. | [`charts/darwin-platform/README.md`](charts/darwin-platform/README.md) |
| [`terraform/`](terraform/)   | **Provisioning** — Terraform envs + modules (kind local, Hetzner k3s prod, shared `platform-stack`). | [`terraform/README.md`](terraform/README.md)     |
| [`grafana/`](grafana/)       | Dashboard JSON for platform metrics.                                                           | [`grafana/README.md`](grafana/README.md)         |
| [`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md) | Provider matrix, required env vars, and the failure-response checklist for operators. | —                                                |

## How the layers relate

- **`docker/`** — fastest no-Kubernetes loop; full observability stack.
- **`charts/`** — authoritative Kubernetes workload spec (supervisor, egress proxy, NetworkPolicy, …).
- **`terraform/`** — provisions a cluster and installs the chart via `platform-stack`. Two envs today:
  - **`terraform/envs/local/`** — kind on your laptop.
  - **`terraform/envs/hetzner/`** — Hetzner Cloud k3s + gVisor + raft Vault + TLS (production backend).

The web dashboard ships separately (Vercel in production; locally via Compose or `make dev`).

## Common entry points (from the repo root)

```bash
make dev              # recommended: kind + platform-stack + dashboard (:3000 / :8000)
make dev-down         # tear down kind stack + dashboard
make dev-kubeconfig   # print KUBECONFIG for the local kind cluster

make docker-up        # Compose-only stack (no Kubernetes)
make helm-lint        # lint infra/charts/darwin-platform/

make -C infra/terraform up      # terraform apply local env only (no image build / dashboard)
make -C infra/terraform down
```

## Production deploy (Hetzner)

Cluster + platform apply via Terraform; images come from GHCR on release.

- Env docs: [`terraform/envs/hetzner/README.md`](terraform/envs/hetzner/README.md)
- CI workflow: [`.github/workflows/deploy-hetzner.yml`](../.github/workflows/deploy-hetzner.yml) (`hetzner-prod` GitHub Environment)

Post-apply (manual): DNS A record → LoadBalancer IP, Vault init/unseal + Kubernetes auth (see hetzner README).
