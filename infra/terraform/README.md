# Darwin — Infrastructure as code

Cloud-agnostic Terraform that brings up the Darwin platform on any
Kubernetes cluster. Doc 22 names EKS / Aurora / AWS-Vault; we replaced
each with a self-hostable equivalent so the same modules work on a
laptop (kind), a single-VPS k3s install, DigitalOcean Kubernetes,
Hetzner, GKE, AKS, or anything else `kubectl` can reach.

What's provided:

- `envs/local/` — turnkey **local** stack. One `make -C infra/terraform up` and you
  get a kind cluster, CloudNativePG-managed Postgres, HashiCorp Vault
  (dev mode), LiteLLM proxy, and the `darwin-platform` supervisor
  deployment.
- `modules/` — reusable building blocks. Each module assumes only
  `kubernetes` + `helm` + `kubectl` Terraform providers pointed at an
  existing cluster, so they drop into any new env. The kind cluster
  module is the only one that requires the `tehcyx/kind` provider.

What's NOT provided (intentionally):

- AWS / GCP / Azure cluster modules. The doc-22 EKS module was a
  deployment specialisation, not a feature. Pointing the platform
  layer at a managed K8s cluster you've provisioned out-of-band (DOKS,
  GKE, AKS, k3s) is the supported production path; we can ship a
  cloud-specific cluster wrapper alongside `kind-cluster/` if you
  decide on a primary host.
- Production-grade Vault (raft storage, auto-unseal). The local stack
  runs Vault in dev mode (in-memory, auto-unsealed) because that's
  what makes the laptop loop tight. A `modules/vault-prod/` with raft
  + cloud-KMS auto-unseal is a future addition.

## Prerequisites

- Terraform ≥ 1.6
- Docker (for kind)
- `kind` ≥ 0.24 (boot detection regex on cgroup v2 hosts needs ≥ 0.24).
  Tested with kind 0.24.0 + `kindest/node:v1.31.0`.
- kubectl ≥ 1.27
- ~6 GB free RAM for the local stack

**inotify limits** — kind nodes consume `fs.inotify.max_user_instances`
heavily (≥3 per node). On hosts where the limit hasn't been raised from
the default (often 128 on Linux), kind's nodes fail to boot with the
opaque `could not find a log line that matches …Multi-User System…`
error. The real message in the failed node's logs is:

```
Failed to create control group inotify object: Too many open files
```

Fix per [kind's docs](https://kind.sigs.k8s.io/docs/user/known-issues/#pod-errors-due-to-too-many-open-files):

```bash
# Persistent — survives reboot.
sudo tee /etc/sysctl.d/99-kind.conf <<'EOF'
fs.inotify.max_user_watches=524288
fs.inotify.max_user_instances=512
EOF
sudo sysctl --system

# Or, transient, just for this session:
sudo sysctl fs.inotify.max_user_instances=512
sudo sysctl fs.inotify.max_user_watches=524288
```

Confirm with `sysctl fs.inotify`. Without this, `terraform apply` will
plan + init cleanly but the `null_resource.cluster` step (which shells
out to `kind create cluster`) will fail.

## Quickstart — local

```bash
make -C infra/terraform up           # brings everything online (~3-5 min cold start)
make -C infra/terraform kubeconfig   # echoes KUBECONFIG export for your shell
make -C infra/terraform logs SVC=darwin-platform
make -C infra/terraform down         # tears the stack down + deletes the kind cluster
```

The platform's REST API is reachable at `http://localhost:8000` after
`make up` finishes (kind maps port 80 inside the cluster to 8000 on the
host via an extraPortMapping).

## What gets installed

| Layer            | Resource                                             | Module                    |
| ---------------- | ---------------------------------------------------- | ------------------------- |
| Cluster          | 1 control-plane + 2 worker kind nodes                | `modules/kind-cluster/`   |
| Postgres         | CloudNativePG operator + 1-instance Cluster CR       | `modules/cnpg/`           |
| Secrets          | HashiCorp Vault (dev mode, in-memory)                | `modules/vault/`          |
| LLM router       | LiteLLM proxy Deployment + Service + ConfigMap       | `modules/litellm/`        |
| Platform         | `darwin-platform` supervisor Deployment + RBAC       | `modules/darwin-platform/`|

The supervisor's K8s driver creates one namespace per Darwin (per doc
08) — `modules/darwin-platform/` grants it the cluster-scoped RBAC
needed to do that (create/delete namespaces; within each, manage
configmaps, secrets, PVCs, services, deployments, network policies).

## State

`envs/local/` uses local state (`terraform.tfstate`). A production env
should point `terraform { backend "s3"|"gcs"|"azurerm" {…} }` at remote
state. We deliberately did not pre-pick a backend so the choice tracks
wherever you choose to host the cluster.
