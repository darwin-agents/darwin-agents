# Darwin on Hetzner Cloud (k3s)

The cloud twin of `envs/local`. Same `platform-stack` module; the only
differences are the cluster (hcloud + k3s + gVisor instead of kind) and the
provider knobs (raft Vault, `hcloud-volumes` storage, LoadBalancer ingress,
cert-manager TLS).

```
module.cluster (cluster-hetzner-k3s)  →  3× cax21 ARM, k3s, gVisor on every node
module.hcloud_bootstrap               →  hcloud CCM (LBs + clears node taint) + CSI (storage)
module.platform_stack                 →  cnpg + vault(raft) + litellm + supervisor + ingress(LB+TLS)
```

## Prerequisites

- A Hetzner Cloud project + API token (Read+Write).
- A Hetzner Object Storage bucket + S3 access keys (for remote state).
- An SSH keypair; its public key goes in `ssh_public_key`.
- A domain you control, with an A record you can point at the LoadBalancer IP.
- Terraform ≥ 1.6, `kubectl`, `helm`, `vault` CLI (for the unseal runbook).

## 1. Configure

```bash
cp terraform.tfvars.example terraform.tfvars   # edit domain, acme_email, ssh_public_key, ...
export TF_VAR_hcloud_token=...                  # never commit secrets
export TF_VAR_ssh_private_key="$(cat ~/.ssh/darwin_ed25519)"
```

Remote state (recommended): `cp backend.tf.example backend.tf`, edit
bucket/region/endpoint, then set the S3 creds + the **mandatory** checksum env
vars (Terraform ≥ 1.11.2 breaks Hetzner S3 otherwise):

```bash
export AWS_ACCESS_KEY_ID=<objstore-key>  AWS_SECRET_ACCESS_KEY=<objstore-secret>
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
export AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
terraform init -migrate-state
```

## 2. Apply

```bash
terraform init
terraform apply        # ~5-10 min: servers boot, k3s + gVisor install, addons + stack roll out
```

The cluster is briefly "broken" (nodes `NotReady`/`Pending`) between k3s install
and the CCM clearing the `node.cloudprovider.kubernetes.io/uninitialized` taint
— this is expected.

## 3. DNS

```bash
export KUBECONFIG=$(terraform output -raw kubeconfig_path)
kubectl -n ingress-nginx get svc ingress-nginx-controller   # note EXTERNAL-IP
```

Create an A record: `<domain> → <EXTERNAL-IP>`, wait for propagation. cert-manager
issues the TLS cert via HTTP-01 once the record resolves and port 80 is reachable.
Tip: set `acme_server` to the Let's Encrypt **staging** URL first to avoid burning
prod rate limits, confirm a cert issues, then switch back to prod.

## 4. Vault runbook (raft, no KMS → manual unseal)

Vault boots **sealed** and stays sealed after every pod restart. Initialise once:

```bash
kubectl -n vault exec -ti vault-0 -- vault operator init \
  -key-shares=5 -key-threshold=3 -format=json > vault-init.json   # STORE OFFLINE, SPLIT
# Unseal each pod with 3 of the 5 keys:
for p in vault-0 vault-1 vault-2; do
  [ "$p" = vault-0 ] || kubectl -n vault exec -ti $p -- vault operator raft join http://vault-0.vault-internal:8200
  for k in KEY1 KEY2 KEY3; do kubectl -n vault exec -ti $p -- vault operator unseal "$k"; done
done
kubectl -n vault exec -ti vault-0 -- vault operator raft list-peers   # expect 3 voters
```

Wire the supervisor's Kubernetes auth (run with the root token, then revoke it):

```bash
export VAULT_TOKEN=$(jq -r .root_token vault-init.json)
kubectl -n vault exec -ti vault-0 -- vault secrets enable -path=secret kv-v2
kubectl -n vault exec -ti vault-0 -- sh -c 'cat <<EOF | vault policy write darwin-kv -
path "secret/data/darwins/*"     { capabilities = ["create","read","update","delete"] }
path "secret/metadata/darwins/*" { capabilities = ["list","delete"] }
EOF'
kubectl -n vault exec -ti vault-0 -- vault auth enable kubernetes
kubectl -n vault exec -ti vault-0 -- sh -c \
  'vault write auth/kubernetes/config kubernetes_host=https://$KUBERNETES_PORT_443_TCP_ADDR:443'
kubectl -n vault exec -ti vault-0 -- vault write auth/kubernetes/role/darwin \
  bound_service_account_names=darwin-platform \
  bound_service_account_namespaces=darwin policies=darwin-kv ttl=1h
kubectl -n vault exec -ti vault-0 -- vault token revoke -self   # drop root after bootstrap
```

The supervisor reads `DARWIN_VAULT_URL` (the in-cluster Vault address) and should
authenticate via the Kubernetes auth method (`auth_method=kubernetes`, role
`darwin`) rather than a static token — `HvacVaultClient` already supports this.

> Store the 5 unseal keys + initial root token **offline and split** across
> separate custodians. Never in this cluster, in git, or in the state bucket.
> Losing the key threshold = permanently sealed data.

### 4b. Auto-unseal (optional, doc 20)

Manual unseal means a human after every pod restart. To make restarts
self-heal, configure a **seal stanza** so Vault unseals from an external key.
Cloud-agnostic recommended path: **transit auto-unseal** against a small
bootstrap Vault (or any Vault you trust) that holds the unseal key.

`terraform.tfvars`:

```hcl
vault_seal_config_hcl = <<-HCL
  seal "transit" {
    address    = "https://unsealer.example.com:8200"
    key_name   = "darwin-autounseal"
    mount_path = "transit/"
  }
HCL
# Source the seal's credentials from a Secret (never in tfvars/state):
#   kubectl -n vault create secret generic vault-seal --from-literal=token=<transit-token>
vault_seal_env = [
  { env_name = "VAULT_TOKEN", secret_name = "vault-seal", secret_key = "token" },
]
```

`vault operator init` is still a one-time step; thereafter a restarted pod
unseals itself (no human). With auto-unseal the init produces *recovery* keys
rather than unseal keys. (Cloud KMS — `seal "awskms"|"gcpckms"|"azurekeyvault"`
— works the same way where a KMS is available.)

## 5. Verify gVisor (default per-Darwin isolation)

```bash
kubectl get runtimeclass gvisor                 # handler: runsc
# Spawn a darwin and confirm its orchestrator pod runs under gVisor:
kubectl -n darwin-<id> get pod -o jsonpath='{.items[0].spec.runtimeClassName}'   # gvisor
```

gVisor (a userspace kernel) is the default because Hetzner Cloud VMs have **no
nested virtualization**. Per-neuron *code execution* gets a second, stronger
boundary via E2B Firecracker microVMs (the production sandbox adapter).

### 5b. Kata true microVMs (optional, doc 17)

For a hardware-virtualized boundary (each pod in its own QEMU microVM), enable
Kata — but **only on nested-virt-capable nodes** (`/dev/kvm` present: bare
metal or an instance type with KVM passthrough; stock Hetzner Cloud shared/
dedicated vCPU does not qualify). The node bootstrap installs Kata + registers
the containerd `kata` handler when `kata_enabled=true`; point the runtime class
at it:

```hcl
# cluster module:
kata_enabled = true            # installs kata-static + containerd `kata` handler
# platform-stack:
runtime_class   = "kata"       # per-Darwin pods → runtimeClassName: kata
runtime_handler = "kata"
```

```bash
kubectl get runtimeclass kata
kubectl -n darwin-<id> get pod -o jsonpath='{.items[0].spec.runtimeClassName}'   # kata
```

If `/dev/kvm` is absent the node logs a skip (it does NOT fail to boot); pods
requesting `runtimeClassName: kata` then stay Pending — keep gVisor in that case.

## 6. Postgres PITR (optional, doc 20)

Continuous WAL archiving + scheduled base backups to S3-compatible object
storage give point-in-time recovery. Create a bucket + a credentials Secret in
the platform namespace, then enable it:

```bash
kubectl -n darwin create secret generic pg-backup-s3 \
  --from-literal=ACCESS_KEY_ID=<key> --from-literal=ACCESS_SECRET_KEY=<secret>
```

```hcl
# platform-stack:
database_backup_enabled               = true
database_backup_destination_path      = "s3://my-bucket/darwin-pg"
database_backup_endpoint_url          = "https://fsn1.your-objectstorage.com"  # Hetzner Object Storage
database_backup_s3_credentials_secret = "pg-backup-s3"
database_backup_retention_policy      = "30d"
```

CNPG archives WAL continuously and takes a base backup daily (and once
immediately). **Restore** to a point in time = bootstrap a *new* Cluster with
`bootstrap.recovery` pointing at the same object store and a
`recoveryTarget.targetTime`:

```yaml
spec:
  bootstrap:
    recovery:
      source: darwin-platform-db
      recoveryTarget: { targetTime: "2026-06-03 12:00:00+00" }
  externalClusters:
    - name: darwin-platform-db
      barmanObjectStore:
        destinationPath: s3://my-bucket/darwin-pg
        endpointURL: https://fsn1.your-objectstorage.com
        s3Credentials:
          accessKeyId:     { name: pg-backup-s3, key: ACCESS_KEY_ID }
          secretAccessKey: { name: pg-backup-s3, key: ACCESS_SECRET_KEY }
```

Verify backups: `kubectl -n darwin get backups.postgresql.cnpg.io`.

## Teardown

```bash
terraform destroy
```
