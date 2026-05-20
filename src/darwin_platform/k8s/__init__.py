"""Kubernetes-driver shape for per-Darwin deployment (docs/engineering/08,
docs/overview/07).

Each running Darwin gets its own ``darwin-{id}`` namespace containing:

- The orchestrator pod (one Deployment, ``runtimeClassName: kata`` for
  microVM isolation) running ``python -m darwin_platform.orchestrator``.
- A per-Darwin LanceDB sidecar in the same pod with a PVC for ``/data``.
- A ConfigMap holding the sealed DNA (manifest + layer blobs) so the
  orchestrator boots with the supervisor-minted identity, not a fresh one.
- A Secret holding the per-Darwin control-API bearer token.
- A Service exposing the orchestrator's control API to the supervisor pod.
- A NetworkPolicy defaulting deny and permitting only:
  - DNS (cluster CoreDNS)
  - Supervisor → orchestrator on the control port
  - Orchestrator → ``platform-data/egress-proxy`` for all outbound
  - Orchestrator → Postgres (audit + billing writes)

The supervisor pod uses a ServiceAccount with a ClusterRole scoped to
verbs on these resource types in any namespace matching the prefix.
"""

from .driver import K8sDarwinDriver
from .manifests import (
    NAMESPACE_LABEL_DARWIN_ID,
    NAMESPACE_LABEL_MANAGED_BY,
    build_configmap,
    build_deployment,
    build_namespace,
    build_network_policy,
    build_pvc,
    build_secret,
    build_service,
)

__all__ = [
    "K8sDarwinDriver",
    "NAMESPACE_LABEL_DARWIN_ID",
    "NAMESPACE_LABEL_MANAGED_BY",
    "build_configmap",
    "build_deployment",
    "build_namespace",
    "build_network_policy",
    "build_pvc",
    "build_secret",
    "build_service",
]
