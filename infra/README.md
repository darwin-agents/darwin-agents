# Darwin — infrastructure

Everything needed to build, run, and deploy the Darwin platform, grouped
by deployment target. Pick the subdir that matches where you're running.

| Path                         | What it is                                                                                     | Start here                                       |
| ---------------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| [`docker/`](docker/)         | **Local dev** — Docker Compose stack (supervisor + Postgres + LiteLLM + Prometheus + Jaeger). No Kubernetes. | [`docker/README.md`](docker/README.md)           |
| [`charts/`](charts/)         | **Packaging** — the `darwin-platform` Helm chart. The single source of truth for the Kubernetes workload. | [`charts/darwin-platform/README.md`](charts/darwin-platform/README.md) |
| [`terraform/`](terraform/)   | **Provisioning** — cloud-agnostic Terraform that brings up a whole cluster + stack (kind, CloudNativePG, Vault, LiteLLM, platform). | [`terraform/README.md`](terraform/README.md)     |
| [`grafana/`](grafana/)       | Dashboard JSON for platform metrics.                                                           | [`grafana/README.md`](grafana/README.md)         |
| [`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md) | Provider matrix, required env vars, and the failure-response checklist for operators. | —                                                |

## How the three layers relate

- **`docker/`** is the no-Kubernetes laptop loop — the fastest way to see
  the supervisor running end-to-end.
- **`charts/`** packages the supervisor as a Kubernetes workload. It is
  the authoritative spec for what runs in-cluster.
- **`terraform/`** provisions the cluster and its dependencies, then
  deploys the platform onto it. Its `envs/local/` target gives you a
  one-command kind cluster with the full stack.

These are deliberately separate abstraction layers, not duplicates: a
Compose service, a Helm chart, and a Terraform module are three
different ways the same platform gets stood up.

## Common entry points (from the repo root)

```bash
make docker-up            # bring up the local Compose stack (infra/docker/)
make helm-lint            # lint the Helm chart (infra/charts/darwin-platform/)
make -C infra/terraform up   # provision the full local kind stack (infra/terraform/)
```
