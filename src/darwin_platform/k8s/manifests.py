"""Per-Darwin Kubernetes object builders (docs/engineering/08).

Pure functions returning plain ``dict`` manifests so this module has no
hard dependency on the ``kubernetes`` Python client — the dicts hand to
the client's generic ``create_namespaced_*`` calls (which accept any
mapping with the expected fields), and tests can introspect them without
spinning up an API server.

The shape is *opinionated*:

- ``runtimeClassName: kata`` on the orchestrator pod so the genome runs
  in a Firecracker microVM. The chart's ``runtimeclass.yaml`` declares
  the class; this builder references it by name (configurable).
- A per-Darwin PVC mounted at ``/data``: the audit log, checkpoint and
  wallet feed live there, so a restarted pod is the same organism.
- NetworkPolicy default-deny + allow-pinholes: DNS, supervisor→
  orchestrator, orchestrator→LiteLLM, orchestrator→Postgres, and either
  orchestrator→egress-proxy or (no proxy) public HTTPS only.
- Per-Darwin Secret with a control-API bearer token; the supervisor's
  RPC calls present this token, the orchestrator verifies it.

Anything the caller might reasonably want to tune (image references,
resource limits, the Postgres DSN env, namespace prefix, runtime class
name) is a parameter — opinionated defaults but no buried magic.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

# Labels stamped on every per-Darwin namespace; used by the supervisor's
# RBAC ClusterRole selector AND by NetworkPolicy peer matchers so we can
# write "from any pod in a namespace managed-by darwin-platform with
# darwin-id=foo" rules.
NAMESPACE_LABEL_MANAGED_BY = "darwin.io/managed-by"
NAMESPACE_LABEL_DARWIN_ID = "darwin.io/darwin-id"

# Annotation prefix for non-identity metadata (parent_id, opening_usdc,
# spawned_at). Recorded on the namespace itself so an operator can `kubectl
# get ns -o wide` and see the basics without scraping the supervisor.
_ANN_PARENT_ID = "darwin.io/parent-id"
_ANN_OPENING_USDC = "darwin.io/opening-usdc"
_ANN_SPAWNED_AT = "darwin.io/spawned-at"

# Pod labels — used by the per-Darwin NetworkPolicy podSelector and by
# the Service's selector.
POD_LABEL_DARWIN_ID = "darwin.io/darwin-id"
POD_LABEL_COMPONENT = "darwin.io/component"

# The control API port the orchestrator listens on. Bound to localhost in
# the pod by default; the Service exposes it to the cluster on the same
# number for symmetry.
CONTROL_API_PORT = 8001

# The pod's durable volume (audit log, checkpoint, wallet feed).
DATA_MOUNT_PATH = "/data"

# Private/link-local ranges the direct-egress rule must never reach
# (cluster services, node metadata endpoints, carrier-grade NAT).
_PRIVATE_CIDRS = (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "100.64.0.0/10",
)

# In-pod mount point for the mutable soma tree (docs 03/21/05). When soma-merge
# is enabled, a volume is mounted here and the orchestrator is launched with
# ``--soma-root`` pointing at it, so apply_mutation writes promoted patches to
# disk + the death forensic captures real soma contents. The volume is a
# per-Darwin PVC by default (promoted self-mods survive a pod restart); set
# ``DarwinPodConfig.soma_storage_size=""`` to fall back to an ephemeral
# emptyDir for stacks without a dynamic storage provisioner.
SOMA_MOUNT_PATH = "/var/lib/darwin/soma"

# Secret key (within the per-Darwin ``darwin-control`` Secret) under which the
# supervisor stores this Darwin's minted LiteLLM virtual key (doc 13). The
# deployment surfaces it as the DARWIN_LITELLM_MASTER_KEY env via secretKeyRef
# so the pod authenticates as its own key, never the shared fleet master key.
LITELLM_VIRTUAL_KEY_SECRET_KEY = "litellm_virtual_key"

# Secret keys (within the per-Darwin ``darwin-control`` Secret) under which the
# supervisor seals this Darwin's tenant-namespace credentials (doc 22): the
# per-Darwin Vault AppRole (role_id + secret_id) the NamespaceProvisioner
# minted, and its private organism-DB DSN. The deployment surfaces them as the
# env vars ``_build_real_factories`` already reads (``DARWIN_VAULT_ROLE_ID`` /
# ``DARWIN_VAULT_SECRET_ID`` → AppRole auth, auto-selected) so the pod
# authenticates to Vault as its OWN scoped identity — refused by Vault on any
# other Darwin's paths — instead of carrying the shared fleet root token.
VAULT_APPROLE_ROLE_ID_SECRET_KEY = "vault_approle_role_id"
VAULT_APPROLE_SECRET_ID_SECRET_KEY = "vault_approle_secret_id"
ORGANISM_DB_DSN_SECRET_KEY = "organism_db_dsn"


@dataclass(frozen=True)
class DarwinPodConfig:
    """Tunables for the orchestrator pod's runtime shape. Defaults are
    appropriate for a small Darwin running with the deterministic fakes;
    raise resources for real-adapter Darwins running real LLM/browser
    work."""

    orchestrator_image: str = (
        "ghcr.io/quantium-rock/darwin-agent/darwin-platform:latest"
    )
    image_pull_policy: str = "IfNotPresent"
    image_pull_secrets: tuple[str, ...] = ()
    runtime_class_name: str | None = "kata"
    # The pod's durable /data volume.
    data_storage_size: str = "1Gi"
    data_storage_class: str | None = None
    # Seconds between heartbeats. Each heartbeat may make paid LLM calls.
    heartbeat_seconds: float = 5.0
    # Where LiteLLM runs (NetworkPolicy egress peer). Empty namespace → no
    # rule (an external https LiteLLM rides the public-HTTPS rule).
    llm_namespace: str = ""
    llm_port: int = 4000
    llm_pod_label_key: str = "app.kubernetes.io/name"
    llm_pod_label_value: str = "litellm"
    # Mutable soma tree (docs 03/21/05). When soma-merge is enabled the pod
    # gets a per-Darwin PVC so promoted self-modifications SURVIVE a pod
    # restart (the genome keeps its evolved code, not just within one pod's
    # life). Set ``soma_storage_size=""`` to fall back to an ephemeral
    # emptyDir — for local/kind stacks without a dynamic storage provisioner,
    # where forensics still work (the supervisor pulls before reap) but
    # restarts reset the soma.
    soma_storage_size: str = "1Gi"
    soma_storage_class: str | None = None
    # Resources for the orchestrator container. The orchestrator loads the
    # genome + LLM/adapter SDKs at boot; 1Gi OOM-killed it on the local
    # k8s stack (crash loop, exit 137), so the floor is 2Gi. Override via
    # --k8s-orchestrator-memory.
    orchestrator_cpu_request: str = "100m"
    orchestrator_cpu_limit: str = "1000m"
    orchestrator_memory_request: str = "512Mi"
    orchestrator_memory_limit: str = "2Gi"
    # Where the egress proxy lives (NetworkPolicy peer). Empty → no proxy:
    # pods get direct egress to public HTTPS only.
    platform_data_namespace: str = "platform-data"
    egress_proxy_pod_label_key: str = "darwin.io/component"
    egress_proxy_pod_label_value: str = "egress-proxy"
    # The supervisor's pod selector — used by the orchestrator-ingress
    # NetworkPolicy rule. Default matches the chart's selectorLabels.
    supervisor_namespace: str = "default"
    supervisor_pod_label_key: str = "app.kubernetes.io/name"
    supervisor_pod_label_value: str = "darwin-platform"
    # Postgres egress peer. Empty disables the rule (orchestrator falls
    # back to SQLite, intended for dev/test only).
    postgres_namespace: str = ""
    postgres_pod_label_key: str = ""
    postgres_pod_label_value: str = ""
    postgres_port: int = 5432
    # Service account for the orchestrator pod — created elsewhere
    # (typically in the per-Darwin namespace by the supervisor when it
    # provisions the resources). ``None`` falls back to ``default``.
    service_account_name: str | None = None
    # Free-form extra env vars for the orchestrator container — useful
    # for OTel endpoints, real-adapter API keys via secretKeyRef, etc.
    extra_env: tuple[dict[str, Any], ...] = ()
    # Wallet provider forwarded to the orchestrator pod (--wallet-provider
    # flag). Matches the supervisor's choice so per-pod wallets pick the
    # same backend (cdp / local-vault / web3). Default 'cdp' matches the
    # supervisor CLI default.
    wallet_provider: str = "cdp"
    # Platform-mediated money rails (fiat / sanctions / KYC / omnibus) as a
    # JSON blob, forwarded from the supervisor so the pod's planner reasons
    # over the full money picture (these rails run in the supervisor, never
    # in-pod, so the pod's own vantage would otherwise report ``none``).
    # Emitted as DARWIN_PLATFORM_MONEY_RAILS; empty string → not forwarded.
    platform_money_rails: str = ""


def namespace_name(prefix: str, darwin_id: str) -> str:
    """Stable namespace name for a Darwin. Lower-cased, prefix-bounded,
    and truncated to K8s' 63-char limit."""
    raw = f"{prefix}{darwin_id}".lower()
    return raw[:63]


def build_namespace(
    *,
    darwin_id: str,
    namespace_prefix: str = "darwin-",
    parent_id: str | None = None,
    opening_usdc: float | None = None,
    spawned_at: float | None = None,
) -> dict[str, Any]:
    """Per-Darwin Namespace. Labels are what RBAC + NetworkPolicy match
    against; annotations are operator-visible metadata only."""
    ns = namespace_name(namespace_prefix, darwin_id)
    annotations: dict[str, str] = {}
    if parent_id is not None:
        annotations[_ANN_PARENT_ID] = parent_id
    if opening_usdc is not None:
        annotations[_ANN_OPENING_USDC] = f"{opening_usdc:g}"
    if spawned_at is not None:
        annotations[_ANN_SPAWNED_AT] = f"{spawned_at:.6f}"
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": ns,
            "labels": {
                NAMESPACE_LABEL_MANAGED_BY: "darwin-platform",
                NAMESPACE_LABEL_DARWIN_ID: darwin_id,
                # Honour pod-security standards by default; the
                # orchestrator pod runs non-root, drops caps, etc.
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/audit": "restricted",
            },
            "annotations": annotations,
        },
    }


def build_configmap(
    *,
    darwin_id: str,
    namespace: str,
    manifest_json: str | None,
    layer_blobs: dict[str, bytes],
    layer_names: list[str],
    extra_data: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Mounts at ``/etc/darwin`` in the orchestrator pod.

    - ``manifest.json`` — the sealed DNA manifest the platform signed.
    - ``layer_names.json`` — ordered names so the orchestrator
      reconstructs the layer stack in the right order.
    - ``layers/<name>`` — one binary key per layer blob (base64 in
      ``binaryData``).
    - ``meta.json`` — extra non-secret config the orchestrator reads.

    A ConfigMap is OK for these because none of them are secrets — the
    signed manifest is public, layer blobs are deterministic from it,
    and the meta is plain config. Secrets (the control-API token, real
    provider keys) go through :func:`build_secret`.
    """
    import json

    data: dict[str, str] = {}
    binary_data: dict[str, str] = {}
    if manifest_json is not None:
        data["manifest.json"] = manifest_json
    if layer_names:
        data["layer_names.json"] = json.dumps(list(layer_names))
    for name, blob in layer_blobs.items():
        binary_data[f"layer-{name}"] = base64.b64encode(blob).decode("ascii")
    meta = {"darwin_id": darwin_id}
    if extra_data:
        meta.update(extra_data)
    data["meta.json"] = json.dumps(meta, sort_keys=True)
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "darwin-dna",
            "namespace": namespace,
            "labels": {
                NAMESPACE_LABEL_MANAGED_BY: "darwin-platform",
                NAMESPACE_LABEL_DARWIN_ID: darwin_id,
            },
        },
        "data": data,
        "binaryData": binary_data,
    }


def build_secret(
    *,
    darwin_id: str,
    namespace: str,
    control_token: str,
    extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Per-Darwin control-API bearer token + optional extras (real
    provider API keys passed via env from this Secret). All values
    base64-encoded into ``data`` per the Secret schema."""
    encoded: dict[str, str] = {
        "control_token": base64.b64encode(control_token.encode("utf-8")).decode("ascii"),
    }
    for k, v in (extra or {}).items():
        encoded[k] = base64.b64encode(v.encode("utf-8")).decode("ascii")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "darwin-control",
            "namespace": namespace,
            "labels": {
                NAMESPACE_LABEL_MANAGED_BY: "darwin-platform",
                NAMESPACE_LABEL_DARWIN_ID: darwin_id,
            },
        },
        "type": "Opaque",
        "data": encoded,
    }


def build_pvc(
    *,
    darwin_id: str,
    namespace: str,
    config: DarwinPodConfig,
) -> dict[str, Any]:
    """The pod's durable ``/data`` volume (audit log, checkpoint, wallet
    feed). ReadWriteOnce — the orchestrator is the only writer."""
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": config.data_storage_size}},
    }
    if config.data_storage_class:
        spec["storageClassName"] = config.data_storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": "darwin-data",
            "namespace": namespace,
            "labels": {
                NAMESPACE_LABEL_MANAGED_BY: "darwin-platform",
                NAMESPACE_LABEL_DARWIN_ID: darwin_id,
            },
        },
        "spec": spec,
    }


def build_soma_pvc(
    *,
    darwin_id: str,
    namespace: str,
    config: DarwinPodConfig,
) -> dict[str, Any]:
    """Per-Darwin soma volume (docs 03/21/05). ReadWriteOnce — the
    orchestrator is the only writer. Persists the soma tree (the patches
    apply_mutation lands) across pod restarts so the genome keeps its evolved
    code; it rides the dead-Darwin retention window with the rest of the
    namespace and is destroyed on teardown."""
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": config.soma_storage_size}},
    }
    if config.soma_storage_class:
        spec["storageClassName"] = config.soma_storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": "darwin-soma",
            "namespace": namespace,
            "labels": {
                NAMESPACE_LABEL_MANAGED_BY: "darwin-platform",
                NAMESPACE_LABEL_DARWIN_ID: darwin_id,
            },
        },
        "spec": spec,
    }


def build_service(
    *,
    darwin_id: str,
    namespace: str,
) -> dict[str, Any]:
    """ClusterIP service exposing the orchestrator's control API. The
    supervisor's RPC calls hit ``http://darwin-control.<ns>.svc:8001``."""
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "darwin-control",
            "namespace": namespace,
            "labels": {
                NAMESPACE_LABEL_MANAGED_BY: "darwin-platform",
                NAMESPACE_LABEL_DARWIN_ID: darwin_id,
            },
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {
                POD_LABEL_DARWIN_ID: darwin_id,
                POD_LABEL_COMPONENT: "orchestrator",
            },
            "ports": [
                {
                    "name": "control",
                    "port": CONTROL_API_PORT,
                    "targetPort": "control",
                    "protocol": "TCP",
                },
            ],
        },
    }


def build_deployment(
    *,
    darwin_id: str,
    namespace: str,
    config: DarwinPodConfig,
    opening_usdc: float,
    escalation_auto: str | None,
    database_url_secret_ref: tuple[str, str] | None = None,
    litellm_virtual_key: bool = False,
    vault_approle: bool = False,
    organism_db_dsn: bool = False,
    soma_enabled: bool = False,
) -> dict[str, Any]:
    """Orchestrator pod = the genome's heart. One Deployment, one
    replica (per-Darwin singleton; HA is at the supervisor layer via the
    existing Postgres advisory lock, not by running two orchestrators
    for the same Darwin)."""

    args = [
        "python", "-m", "darwin_platform.runtime.orchestrator",
        "--darwin-id", darwin_id,
        "--manifest-path", "/etc/darwin/manifest.json",
        "--layer-blob-dir", "/etc/darwin",
        "--layer-names-path", "/etc/darwin/layer_names.json",
        "--opening-usdc", f"{opening_usdc:g}",
        "--control-port", str(CONTROL_API_PORT),
        "--control-token-path", "/run/secrets/darwin-control/control_token",
        "--wallet-provider", config.wallet_provider,
        "--heartbeat-seconds", f"{config.heartbeat_seconds:g}",
    ]
    # Always explicit: None means "owner responds", never the pod default.
    args.extend(["--escalation-auto", escalation_auto or "none"])
    # Real soma merge + death forensics (docs 03/21/05). The supervisor enables
    # this when it's configured with a soma root; the pod writes its soma tree
    # to the SOMA_MOUNT_PATH emptyDir below.
    if soma_enabled:
        args.extend(["--soma-root", SOMA_MOUNT_PATH])

    env: list[dict[str, Any]] = [
        {"name": "DARWIN_ID", "value": darwin_id},
        {"name": "DARWIN_NAMESPACE", "value": namespace},
    ]
    if database_url_secret_ref is not None:
        secret_name, secret_key = database_url_secret_ref
        env.append({
            "name": "DATABASE_URL",
            "valueFrom": {
                "secretKeyRef": {"name": secret_name, "key": secret_key},
            },
        })
    # Per-Darwin LiteLLM virtual key (doc 13). Sourced from this Darwin's own
    # ``darwin-control`` Secret and surfaced as DARWIN_LITELLM_MASTER_KEY, so
    # every LiteLLM call the pod makes (chat + embeddings) authenticates as
    # the Darwin's own key. The supervisor omits the shared fleet master key
    # from config.extra_env when virtual keys are on, so there is no env-name
    # collision.
    if litellm_virtual_key:
        env.append({
            "name": "DARWIN_LITELLM_MASTER_KEY",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "darwin-control",
                    "key": LITELLM_VIRTUAL_KEY_SECRET_KEY,
                },
            },
        })
    # Per-Darwin Vault AppRole (doc 22). Surfaced under the exact env names
    # ``_build_real_factories`` reads (DARWIN_VAULT_ROLE_ID / _SECRET_ID), which
    # auto-selects ``auth_method=approle`` — so the pod's HvacVaultClient logs
    # in as this Darwin's own scoped role, refused by Vault on other Darwins'
    # paths. The supervisor omits the shared fleet root token from the pod's env
    # when this is on, so the pod never holds a cross-tenant credential.
    if vault_approle:
        env.append({
            "name": "DARWIN_VAULT_ROLE_ID",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "darwin-control",
                    "key": VAULT_APPROLE_ROLE_ID_SECRET_KEY,
                },
            },
        })
        env.append({
            "name": "DARWIN_VAULT_SECRET_ID",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "darwin-control",
                    "key": VAULT_APPROLE_SECRET_ID_SECRET_KEY,
                },
            },
        })
    # Per-Darwin organism DB DSN (doc 22). Delivered to the pod for the
    # organism-data reader (dashboard / business-data neurons) to consume; the
    # private database + owner role were provisioned by the NamespaceProvisioner.
    if organism_db_dsn:
        env.append({
            "name": "DARWIN_ORGANISM_DB_DSN",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "darwin-control",
                    "key": ORGANISM_DB_DSN_SECRET_KEY,
                },
            },
        })
    # Platform-mediated money rails (fiat / sanctions / KYC / omnibus). The
    # orchestrator folds these into its money-capabilities manifest so the
    # pod's planner sees the same rails the operator endpoint reports.
    if config.platform_money_rails:
        env.append({
            "name": "DARWIN_PLATFORM_MONEY_RAILS",
            "value": config.platform_money_rails,
        })
    env.extend(list(config.extra_env))

    orchestrator_container = {
        "name": "orchestrator",
        "image": config.orchestrator_image,
        "imagePullPolicy": config.image_pull_policy,
        "args": args,
        "env": env,
        "ports": [
            {"name": "control", "containerPort": CONTROL_API_PORT,
             "protocol": "TCP"},
        ],
        "readinessProbe": {
            "httpGet": {"path": "/healthz", "port": "control"},
            "initialDelaySeconds": 3,
            "periodSeconds": 5,
            "timeoutSeconds": 3,
            "failureThreshold": 3,
        },
        "livenessProbe": {
            "httpGet": {"path": "/healthz", "port": "control"},
            "initialDelaySeconds": 30,
            "periodSeconds": 30,
            "timeoutSeconds": 5,
            "failureThreshold": 5,
        },
        "resources": {
            "requests": {
                "cpu": config.orchestrator_cpu_request,
                "memory": config.orchestrator_memory_request,
            },
            "limits": {
                "cpu": config.orchestrator_cpu_limit,
                "memory": config.orchestrator_memory_limit,
            },
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "readOnlyRootFilesystem": False,
        },
        "volumeMounts": [
            {"name": "darwin-dna", "mountPath": "/etc/darwin", "readOnly": True},
            {"name": "darwin-control", "mountPath": "/run/secrets/darwin-control",
             "readOnly": True},
            {"name": "darwin-data", "mountPath": DATA_MOUNT_PATH},
        ],
    }
    if soma_enabled:
        orchestrator_container["volumeMounts"].append(
            {"name": "darwin-soma", "mountPath": SOMA_MOUNT_PATH},
        )

    containers = [orchestrator_container]
    volumes = [
        {"name": "darwin-dna",
         "configMap": {"name": "darwin-dna"}},
        {"name": "darwin-control",
         "secret": {"secretName": "darwin-control"}},
        {"name": "darwin-data",
         "persistentVolumeClaim": {"claimName": "darwin-data"}},
    ]
    if soma_enabled:
        # Persistent by default: a per-Darwin PVC so promoted self-mods
        # survive a pod restart. emptyDir only when soma storage is explicitly
        # disabled (local/kind stacks with no dynamic provisioner) — forensics
        # still work (pulled before reap), but a restart resets the soma.
        if config.soma_storage_size:
            volumes.append({
                "name": "darwin-soma",
                "persistentVolumeClaim": {"claimName": "darwin-soma"},
            })
        else:
            volumes.append({"name": "darwin-soma", "emptyDir": {}})

    pod_spec: dict[str, Any] = {
        "serviceAccountName": config.service_account_name or "default",
        "automountServiceAccountToken": False,
        "securityContext": {
            "fsGroup": 1000,
            "runAsNonRoot": True,
            "runAsUser": 1000,
            # Pod Security Admission's "restricted" profile (which the
            # namespace labels at creation, line 145 below) requires a
            # seccomp profile of RuntimeDefault or Localhost. Without
            # this every spawn fails admission with "violates PodSecurity
            # restricted:latest".
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": containers,
        "volumes": volumes,
    }
    if config.runtime_class_name:
        pod_spec["runtimeClassName"] = config.runtime_class_name
    if config.image_pull_secrets:
        pod_spec["imagePullSecrets"] = [
            {"name": n} for n in config.image_pull_secrets
        ]

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "darwin-orchestrator",
            "namespace": namespace,
            "labels": {
                NAMESPACE_LABEL_MANAGED_BY: "darwin-platform",
                NAMESPACE_LABEL_DARWIN_ID: darwin_id,
                POD_LABEL_COMPONENT: "orchestrator",
            },
        },
        "spec": {
            "replicas": 1,
            # Per-Darwin singleton — no overlap during rollout.
            "strategy": {"type": "Recreate"},
            "selector": {
                "matchLabels": {
                    POD_LABEL_DARWIN_ID: darwin_id,
                    POD_LABEL_COMPONENT: "orchestrator",
                },
            },
            "template": {
                "metadata": {
                    "labels": {
                        POD_LABEL_DARWIN_ID: darwin_id,
                        POD_LABEL_COMPONENT: "orchestrator",
                        NAMESPACE_LABEL_MANAGED_BY: "darwin-platform",
                    },
                },
                "spec": pod_spec,
            },
        },
    }


def build_network_policy(
    *,
    darwin_id: str,
    namespace: str,
    config: DarwinPodConfig,
) -> dict[str, Any]:
    """Default deny + allow-pinholes.

    Ingress:
      - Supervisor pod → orchestrator's control port.

    Egress:
      - DNS (UDP 53 + TCP 53 to kube-system CoreDNS).
      - Orchestrator → in-cluster LiteLLM (when ``llm_namespace`` is set).
      - Orchestrator → ``platform-data/egress-proxy`` (any TCP) when a
        proxy namespace is configured; otherwise public HTTPS (TCP 443)
        to anything outside the private ranges — never cluster services
        or node metadata.
      - Orchestrator → Postgres (when configured).

    Anything else is dropped."""

    ingress_rules: list[dict[str, Any]] = [
        {
            "from": [
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name":
                                config.supervisor_namespace,
                        },
                    },
                    "podSelector": {
                        "matchLabels": {
                            config.supervisor_pod_label_key:
                                config.supervisor_pod_label_value,
                        },
                    },
                },
            ],
            "ports": [
                {"protocol": "TCP", "port": CONTROL_API_PORT},
            ],
        },
    ]

    egress_rules: list[dict[str, Any]] = [
        # DNS — to kube-system CoreDNS pods. Both UDP and TCP because
        # cluster DNS may fall back to TCP for large responses.
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name": "kube-system",
                        },
                    },
                    "podSelector": {
                        "matchLabels": {"k8s-app": "kube-dns"},
                    },
                },
            ],
            "ports": [
                {"protocol": "UDP", "port": 53},
                {"protocol": "TCP", "port": 53},
            ],
        },
    ]
    if config.platform_data_namespace:
        egress_rules.append({
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name":
                                config.platform_data_namespace,
                        },
                    },
                    "podSelector": {
                        "matchLabels": {
                            config.egress_proxy_pod_label_key:
                                config.egress_proxy_pod_label_value,
                        },
                    },
                },
            ],
        })
    else:
        egress_rules.append({
            "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(_PRIVATE_CIDRS)}}],
            "ports": [{"protocol": "TCP", "port": 443}],
        })
    if config.llm_namespace:
        egress_rules.append({
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name": config.llm_namespace,
                        },
                    },
                    "podSelector": {
                        "matchLabels": {
                            config.llm_pod_label_key: config.llm_pod_label_value,
                        },
                    },
                },
            ],
            "ports": [{"protocol": "TCP", "port": config.llm_port}],
        })
    if (
        config.postgres_namespace
        and config.postgres_pod_label_key
        and config.postgres_pod_label_value
    ):
        egress_rules.append({
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name":
                                config.postgres_namespace,
                        },
                    },
                    "podSelector": {
                        "matchLabels": {
                            config.postgres_pod_label_key:
                                config.postgres_pod_label_value,
                        },
                    },
                },
            ],
            "ports": [
                {"protocol": "TCP", "port": config.postgres_port},
            ],
        })

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": "darwin-default",
            "namespace": namespace,
            "labels": {
                NAMESPACE_LABEL_MANAGED_BY: "darwin-platform",
                NAMESPACE_LABEL_DARWIN_ID: darwin_id,
            },
        },
        "spec": {
            "podSelector": {
                "matchLabels": {
                    NAMESPACE_LABEL_MANAGED_BY: "darwin-platform",
                },
            },
            "policyTypes": ["Ingress", "Egress"],
            "ingress": ingress_rules,
            "egress": egress_rules,
        },
    }


@dataclass(frozen=True)
class DarwinManifestBundle:
    """Everything the K8s driver needs to create for one Darwin, in one
    shot. The driver iterates these in order — namespace first, then
    everything else (which depends on the namespace existing)."""

    namespace: dict[str, Any]
    configmap: dict[str, Any]
    secret: dict[str, Any]
    pvc: dict[str, Any]
    service: dict[str, Any]
    deployment: dict[str, Any]
    network_policy: dict[str, Any]
    # Per-Darwin soma PVC, present only when soma-merge is enabled AND
    # persistent (the default). None → the pod uses an emptyDir (or no soma).
    soma_pvc: dict[str, Any] | None = None


def build_bundle(
    *,
    darwin_id: str,
    namespace_prefix: str,
    opening_usdc: float,
    escalation_auto: str | None,
    parent_id: str | None,
    manifest_json: str | None,
    layer_blobs: dict[str, bytes],
    layer_names: list[str],
    control_token: str,
    config: DarwinPodConfig,
    secret_extras: dict[str, str] | None = None,
    database_url_secret_ref: tuple[str, str] | None = None,
    spawned_at: float | None = None,
    soma_enabled: bool = False,
) -> DarwinManifestBundle:
    # The pod authenticates LiteLLM as its own virtual key when the supervisor
    # sealed one into the per-Darwin Secret (doc 13).
    has_virtual_key = bool(
        secret_extras and secret_extras.get(LITELLM_VIRTUAL_KEY_SECRET_KEY)
    )
    # The pod authenticates to Vault as its own AppRole / has its own organism
    # DB when the NamespaceProvisioner sealed those into the Secret (doc 22).
    has_vault_approle = bool(
        secret_extras
        and secret_extras.get(VAULT_APPROLE_ROLE_ID_SECRET_KEY)
        and secret_extras.get(VAULT_APPROLE_SECRET_ID_SECRET_KEY)
    )
    has_organism_db = bool(
        secret_extras and secret_extras.get(ORGANISM_DB_DSN_SECRET_KEY)
    )
    """Helper composing every per-Darwin object. The driver applies them
    in order: namespace, then configmap/secret/pvc, then service, then
    deployment, then network policy."""
    ns_obj = build_namespace(
        darwin_id=darwin_id,
        namespace_prefix=namespace_prefix,
        parent_id=parent_id,
        opening_usdc=opening_usdc,
        spawned_at=spawned_at,
    )
    namespace = ns_obj["metadata"]["name"]
    return DarwinManifestBundle(
        namespace=ns_obj,
        configmap=build_configmap(
            darwin_id=darwin_id,
            namespace=namespace,
            manifest_json=manifest_json,
            layer_blobs=layer_blobs,
            layer_names=layer_names,
        ),
        secret=build_secret(
            darwin_id=darwin_id,
            namespace=namespace,
            control_token=control_token,
            extra=secret_extras,
        ),
        pvc=build_pvc(
            darwin_id=darwin_id, namespace=namespace, config=config,
        ),
        service=build_service(darwin_id=darwin_id, namespace=namespace),
        deployment=build_deployment(
            darwin_id=darwin_id,
            namespace=namespace,
            config=config,
            opening_usdc=opening_usdc,
            escalation_auto=escalation_auto,
            database_url_secret_ref=database_url_secret_ref,
            litellm_virtual_key=has_virtual_key,
            vault_approle=has_vault_approle,
            organism_db_dsn=has_organism_db,
            soma_enabled=soma_enabled,
        ),
        network_policy=build_network_policy(
            darwin_id=darwin_id, namespace=namespace, config=config,
        ),
        # A persistent soma PVC only when soma-merge is on AND storage is
        # configured; an empty size opts into the ephemeral emptyDir instead.
        soma_pvc=(
            build_soma_pvc(
                darwin_id=darwin_id, namespace=namespace, config=config,
            )
            if soma_enabled and config.soma_storage_size
            else None
        ),
    )
