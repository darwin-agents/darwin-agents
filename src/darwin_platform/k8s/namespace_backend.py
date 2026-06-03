"""K8s side of the per-Darwin :class:`NamespaceProvisioner` (doc 22).

The provisioner orchestrates three per-Darwin namespaces (K8s + Vault +
organism DB). In a k8s deployment the **K8s namespace is already owned by the
:class:`~darwin_platform.k8s.driver.K8sDarwinDriver`**: it creates the
namespace + NetworkPolicy + orchestrator pod atomically at spawn and deletes
the namespace at kill. So the provisioner must *not* create a competing
namespace — its production value-add is the Vault namespace + organism DB the
driver does not provision.

:class:`DriverK8sNamespaceBackend` is the thin, deliberately-passive adapter
that reconciles those two owners:

- :meth:`create_namespace` only *derives* the namespace name (via the same
  :func:`~darwin_platform.k8s.manifests.namespace_name` the driver uses, so the
  recorded :class:`TenantNamespace.k8s_namespace` matches the real one) — it
  does **not** create it. The driver does, when it starts the pod.
- :meth:`apply_network_policy` is a no-op — the driver applies the per-Darwin
  NetworkPolicy as part of the pod bundle.
- :meth:`delete_namespace` is a *tolerant best-effort* delete. The driver
  already deletes the namespace at kill; this is the backstop for the deferred
  90-day reaper (doc 09 #3), which runs long after the driver handle is gone —
  by then the namespace is usually already deleted, so an "already gone" error
  is success.

This keeps a single source of truth for namespace creation (the driver) while
letting the provisioner own the Vault + DB lifecycle and the final cleanup
sweep.
"""

from __future__ import annotations

from typing import Any

from .manifests import namespace_name


class DriverK8sNamespaceBackend:
    """:class:`~darwin_platform.tenancy.namespace_provisioner.K8sNamespaceBackend`
    that defers namespace creation to the driver and only owns tolerant
    teardown.

    Args:
        api: the :class:`~darwin_platform.k8s.driver.KubernetesApi` the driver
            uses (``delete_namespace`` is the only method called here).
        namespace_prefix: must match the driver's prefix so derived names line
            up with the namespaces the driver actually creates.
    """

    def __init__(self, *, api: Any, namespace_prefix: str = "darwin-") -> None:
        self._api = api
        self._prefix = namespace_prefix

    def create_namespace(
        self, *, darwin_id: str, labels: dict[str, str] | None = None,
    ) -> str:
        # Derive only — the driver creates the actual namespace at pod start.
        # Returning the canonical name keeps the TenantNamespace record honest.
        return namespace_name(self._prefix, darwin_id)

    def apply_network_policy(self, *, namespace: str) -> None:
        # The driver applies the per-Darwin NetworkPolicy in its pod bundle.
        return None

    def delete_namespace(self, *, namespace: str) -> None:
        # Best-effort: the driver normally deletes the namespace at kill, so by
        # the time the deferred reaper runs it is usually already gone. Treat
        # an "already gone" / not-found as success; only unexpected errors
        # would matter, and the provisioner's teardown already collects them.
        try:
            self._api.delete_namespace(namespace)
        except Exception as exc:  # noqa: BLE001
            if not _is_not_found(exc):
                raise


def _is_not_found(exc: BaseException) -> bool:
    """A delete against an already-absent namespace is success, not failure.
    The kubernetes client raises ``ApiException(status=404)``; recording fakes
    in tests raise plainer errors whose message says so."""
    status = getattr(exc, "status", None)
    if status == 404 or status == "404":
        return True
    text = str(exc).lower()
    return "not found" in text or "notfound" in text or "404" in text
