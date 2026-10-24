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
- `modules/platform-stack/` — the **cloud-agnostic composition**: it
  wires `cnpg` + `vault` + `litellm` + `darwin-platform` + `k8s-addons`
  into one call, parameterised by provider knobs (image, `runtime_class`,
  `storage_class`, `exposure`, TLS, wallet provider, …). An env supplies
  a cluster + configured providers and calls this module once. Adding a
  provider = a new `envs/<provider>/` that swaps the cluster module and
  passes cloud knobs; **nothing in `platform-stack` changes**.
- `modules/k8s-addons/` — the exposure layer (ingress-nginx controller +
  the platform `Ingress`), toggled by `exposure` (`kind-hostport` |
  `loadbalancer` | `none`) with optional cert-manager TLS for cloud.
- `modules/vault-prod/` — Vault HA with integrated **raft** storage (3
  replicas, persistent volumes). Used when `platform-stack`'s
  `vault_mode = "raft"` (Hetzner). **Manual unseal** — no KMS
  auto-unseal stanza; pods stay `NotReady` until an operator unseals
  them. Init + unseal + Kubernetes auth: [`envs/hetzner/README.md`](envs/hetzner/README.md).
- `envs/hetzner/` — production-shaped stack on Hetzner Cloud (k3s,
  gVisor, hcloud CCM/CSI, LoadBalancer ingress, Let's Encrypt). Remote
  state via Hetzner Object Storage; deploy from CI on release (see
  `.github/workflows/deploy-hetzner.yml`).

What's NOT provided (intentionally):

- AWS / GCP / Azure cluster modules. The doc-22 EKS module was a
  deployment specialisation, not a feature. Pointing the platform
  layer at a managed K8s cluster you've provisioned out-of-band (DOKS,
  GKE, AKS, k3s) is the supported production path; add another
  `envs/<provider>/` sibling that swaps the cluster module.
- Vault **auto-unseal** (cloud KMS). `vault-prod` is raft + manual
  unseal; KMS seal is a future addition if you want unattended restarts.
- `modules/redis` for LiteLLM response cache in-cluster (Compose has
  Redis; TF stacks omit it — see env-specific `litellm.config.yaml`).

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

The turnkey path is the repo-root **`make dev`** — one command builds the
supervisor image, creates the kind cluster, loads the image, applies the whole
stack, and starts the dashboard on `localhost:3000`:

```bash
make dev            # kind + full stack + dashboard (idempotent; re-run to pick up code changes)
make dev-kubeconfig # echoes the KUBECONFIG export for your shell
make dev-down       # tears the stack down + deletes the kind cluster + stops the dashboard
```

`make dev` wraps the three things a bare `terraform apply` can't do on its own:
build + `kind load` the image *before* the supervisor release waits on it,
refresh the Helm repo cache, and start only the dashboard container
(`--no-deps`) so Compose doesn't boot a second supervisor on `:8000`. See
`scripts/dev-up.sh`.

To drive the Terraform env directly (no image build / dashboard):

```bash
make -C infra/terraform up           # brings everything online (~3-5 min cold start)
make -C infra/terraform kubeconfig   # echoes KUBECONFIG export for your shell
make -C infra/terraform logs SVC=darwin-platform
make -C infra/terraform down         # tears the stack down + deletes the kind cluster
```

> Direct `make -C infra/terraform up` on a *fresh* cluster needs the image
> loaded first (`docker build -f infra/docker/Dockerfile -t darwin-platform:dev .`
> then `kind load docker-image darwin-platform:dev --name darwin`) or the
> supervisor release hangs on `ImagePullBackOff`. `make dev` does this for you.

The platform's REST API is reachable at `http://localhost:8000` after
`make up` finishes (kind maps port 80 inside the cluster to 8000 on the
host via an extraPortMapping).

## What gets installed

| Layer            | Resource                                             | Module                     |
| ---------------- | ---------------------------------------------------- | -------------------------- |
| Cluster          | 1 control-plane + 2 worker kind nodes                | `modules/kind-cluster/`    |
| Composition      | Wires the layers below into one call                 | `modules/platform-stack/`  |
| Postgres         | CloudNativePG operator + 1-instance Cluster CR       | `modules/cnpg/`            |
| Secrets          | Vault dev (`vault_mode=dev`) or raft (`vault-prod`)  | `modules/vault/` or `vault-prod/` |
| LLM router       | LiteLLM proxy Deployment + Service + ConfigMap       | `modules/litellm/`         |
| Platform         | `darwin-platform` supervisor (via the Helm chart)    | `modules/darwin-platform/` |
| Exposure         | ingress-nginx + the platform `Ingress`               | `modules/k8s-addons/`      |

`modules/darwin-platform/` is a thin wrapper that installs the
`infra/charts/darwin-platform` Helm chart (the single source of truth
for the supervisor's Deployment, Service, ServiceAccount, and K8s-driver
RBAC) and wires its values to the CNPG / Vault / LiteLLM module outputs.
It keeps one thing in Terraform that the chart deliberately doesn't
model: the supervisor's env Secret (Vault / LiteLLM wiring from sibling
modules plus the deploy's `secret_env` — CDP, KYC, sanctions, MoonPay,
VAPID — mounted via `secretKeyRef`). The API is fronted by `modules/k8s-addons/` (the NGINX
Ingress), kept separate so the exposure strategy is a per-env knob. The
local stack runs the chart's k8s driver mode with the production extras
stripped (no egress proxy / per-Darwin NetworkPolicies / LanceDB
sidecar).

The supervisor's K8s driver creates one namespace per Darwin (per doc
08) — the chart grants it the cluster-scoped RBAC needed to do that
(create/delete namespaces; within each, manage configmaps, secrets,
PVCs, services, deployments, network policies).

## State

| Env | Backend |
| --- | ------- |
| `envs/local/` | Local `terraform.tfstate` |
| `envs/hetzner/` | Remote S3-compatible (Hetzner Object Storage) — see `backend.tf.example` |

Other envs can use any remote backend; the Hetzner env is the reference
for production state + locking (`use_lockfile`).
