"""K8sDarwinDriver — spawns per-Darwin namespaces and drives lifecycle
over HTTP RPC to the orchestrator pods (docs/engineering/08).

This driver is what makes the doc-08 deployment shape real:

- :meth:`start` builds the per-Darwin manifest bundle, applies it via
  the kubernetes API, waits for the orchestrator pod to report ready
  via its control endpoint, and returns the initial snapshot.
- :meth:`pause` / :meth:`resume` / :meth:`kill` / :meth:`force_reproduce`
  /:meth:`snapshot` POST/GET against the orchestrator's control API
  (Service-routed to ``http://darwin-control.<ns>.svc:8001``).
- :meth:`stop` deletes the namespace, which garbage-collects every
  per-Darwin resource (Deployment, Service, ConfigMap, Secret, PVC,
  NetworkPolicy).

The kubernetes API is reached through a narrow :class:`KubernetesApi`
Protocol so tests can pass a fake and we don't grow a hard dependency
on the ``kubernetes`` package at import time.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from ..runtime.driver import (
    DarwinDriver,
    DarwinNotFound,
    DarwinSnapshot,
    DarwinSpec,
    DriverError,
)
from .manifests import (
    CONTROL_API_PORT,
    DarwinManifestBundle,
    DarwinPodConfig,
    build_bundle,
    namespace_name,
)


@runtime_checkable
class KubernetesApi(Protocol):
    """Tiny façade over the kubernetes-python-client surface this driver
    actually uses. The real implementation wraps ``kubernetes.client``;
    tests pass a recording fake."""

    def create_namespace(self, body: dict) -> None: ...
    def create_namespaced_config_map(self, namespace: str, body: dict) -> None: ...
    def create_namespaced_secret(self, namespace: str, body: dict) -> None: ...
    def create_namespaced_persistent_volume_claim(
        self, namespace: str, body: dict,
    ) -> None: ...
    def create_namespaced_service(self, namespace: str, body: dict) -> None: ...
    def create_namespaced_deployment(self, namespace: str, body: dict) -> None: ...
    def create_namespaced_network_policy(self, namespace: str, body: dict) -> None: ...
    def delete_namespace(self, name: str) -> None: ...
    def list_namespaces_by_label(self, label_selector: str) -> list[dict]: ...
    def read_namespaced_deployment_status(
        self, name: str, namespace: str,
    ) -> dict: ...


@runtime_checkable
class ControlClient(Protocol):
    """HTTP RPC to one orchestrator pod's control API. Defaults to an
    httpx-backed client; tests pass a fake."""

    async def health(self, base_url: str, token: str) -> bool: ...
    async def snapshot(
        self, base_url: str, token: str, darwin_id: str,
    ) -> dict: ...
    async def post(
        self,
        base_url: str,
        token: str,
        path: str,
        body: dict | None = None,
    ) -> dict: ...


def _new_control_token() -> str:
    """160 bits of entropy. Long enough that brute-forcing the per-pod
    token over the cluster network is not a concern even at high RPS."""
    return secrets.token_urlsafe(20)


@dataclass
class _K8sHandle:
    """Internal: what the driver remembers for one Darwin."""

    darwin_id: str
    namespace: str
    control_token: str
    spawned_at: float
    last_snapshot: DarwinSnapshot | None = None


class K8sDarwinDriver:
    """The doc-08 production driver — one namespace per Darwin, orchestrator
    pod under ``runtimeClassName: kata`` in a Firecracker microVM."""

    def __init__(
        self,
        *,
        api: KubernetesApi,
        control_client: ControlClient,
        pod_config: DarwinPodConfig | None = None,
        namespace_prefix: str = "darwin-",
        clock: Callable[[], float] = time.time,
        ready_timeout_seconds: float = 120.0,
        ready_poll_interval_seconds: float = 1.0,
        database_url_secret_ref: tuple[str, str] | None = None,
        secret_extras: dict[str, str] | None = None,
    ) -> None:
        self._api = api
        self._control = control_client
        self._config = pod_config or DarwinPodConfig()
        self._prefix = namespace_prefix
        self._clock = clock
        self._ready_timeout = float(ready_timeout_seconds)
        self._ready_poll = float(ready_poll_interval_seconds)
        self._db_secret_ref = database_url_secret_ref
        self._secret_extras = dict(secret_extras or {})
        self._handles: dict[str, _K8sHandle] = {}

    # -- lifecycle -------------------------------------------------------
    async def start(self, spec: DarwinSpec) -> DarwinSnapshot:
        if spec.darwin_id in self._handles:
            raise DriverError(
                f"darwin {spec.darwin_id!r} is already running in this driver"
            )
        token = _new_control_token()
        bundle: DarwinManifestBundle = build_bundle(
            darwin_id=spec.darwin_id,
            namespace_prefix=self._prefix,
            opening_usdc=spec.opening_usdc,
            escalation_auto=spec.escalation_auto,
            parent_id=spec.parent_id,
            manifest_json=spec.manifest_json,
            layer_blobs=spec.layer_blobs,
            layer_names=spec.layer_names,
            control_token=token,
            config=self._config,
            secret_extras=self._secret_extras,
            database_url_secret_ref=self._db_secret_ref,
            spawned_at=self._clock(),
        )
        namespace = bundle.namespace["metadata"]["name"]
        try:
            self._api.create_namespace(bundle.namespace)
            self._api.create_namespaced_config_map(namespace, bundle.configmap)
            self._api.create_namespaced_secret(namespace, bundle.secret)
            self._api.create_namespaced_persistent_volume_claim(
                namespace, bundle.pvc,
            )
            self._api.create_namespaced_service(namespace, bundle.service)
            self._api.create_namespaced_deployment(namespace, bundle.deployment)
            self._api.create_namespaced_network_policy(
                namespace, bundle.network_policy,
            )
        except Exception as exc:
            # Best-effort rollback. If namespace creation itself failed
            # there's nothing to clean up; if a later step failed the
            # namespace delete cascades through every child.
            try:
                self._api.delete_namespace(namespace)
            except Exception:
                pass
            raise DriverError(
                f"failed to provision K8s resources for {spec.darwin_id!r}: {exc}"
            ) from exc
        handle = _K8sHandle(
            darwin_id=spec.darwin_id,
            namespace=namespace,
            control_token=token,
            spawned_at=self._clock(),
        )
        self._handles[spec.darwin_id] = handle
        snapshot = await self._wait_until_ready(handle)
        return snapshot

    async def stop(self, darwin_id: str) -> None:
        handle = self._handles.pop(darwin_id, None)
        if handle is None:
            return  # idempotent
        try:
            self._api.delete_namespace(handle.namespace)
        except Exception as exc:
            raise DriverError(
                f"failed to delete namespace {handle.namespace!r}: {exc}"
            ) from exc

    async def pause(self, darwin_id: str, *, reason: str) -> None:
        await self._post(darwin_id, "/pause", {"reason": reason})

    async def resume(self, darwin_id: str) -> None:
        await self._post(darwin_id, "/resume", {})

    async def kill(self, darwin_id: str, *, reason: str) -> None:
        await self._post(darwin_id, "/kill", {"reason": reason})

    async def force_reproduce(
        self,
        darwin_id: str,
        *,
        amendment_blob: bytes | None = None,
        amendment_name: str | None = None,
        kill_parent_reason: str | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if amendment_blob is not None:
            import base64
            body["amendment_blob_b64"] = base64.b64encode(amendment_blob).decode("ascii")
        if amendment_name is not None:
            body["amendment_name"] = amendment_name
        if kill_parent_reason is not None:
            body["kill_parent_reason"] = kill_parent_reason
        await self._post(darwin_id, "/force-reproduce", body)

    async def snapshot(self, darwin_id: str) -> DarwinSnapshot:
        handle = self._require(darwin_id)
        try:
            data = await self._control.snapshot(
                self._base_url(handle), handle.control_token, darwin_id,
            )
        except Exception as exc:
            raise DriverError(
                f"failed to read snapshot for {darwin_id!r}: {exc}"
            ) from exc
        snap = self._snapshot_from_dict(darwin_id, data)
        handle.last_snapshot = snap
        return snap

    async def list_ids(self) -> list[str]:
        return list(self._handles)

    # -- internals -------------------------------------------------------
    def _require(self, darwin_id: str) -> _K8sHandle:
        h = self._handles.get(darwin_id)
        if h is None:
            raise DarwinNotFound(darwin_id)
        return h

    def _base_url(self, handle: _K8sHandle) -> str:
        return (
            f"http://darwin-control.{handle.namespace}.svc:{CONTROL_API_PORT}"
        )

    async def _post(self, darwin_id: str, path: str, body: dict) -> dict:
        handle = self._require(darwin_id)
        try:
            return await self._control.post(
                self._base_url(handle), handle.control_token, path, body,
            )
        except Exception as exc:
            raise DriverError(
                f"orchestrator RPC {path!r} failed for {darwin_id!r}: {exc}"
            ) from exc

    async def _wait_until_ready(self, handle: _K8sHandle) -> DarwinSnapshot:
        deadline = self._clock() + self._ready_timeout
        last_err: Exception | None = None
        while self._clock() < deadline:
            try:
                ok = await self._control.health(
                    self._base_url(handle), handle.control_token,
                )
            except Exception as exc:
                last_err = exc
                ok = False
            if ok:
                snap = await self.snapshot(handle.darwin_id)
                return snap
            await asyncio.sleep(self._ready_poll)
        # Tear down the half-spun pod so we don't leak a never-ready
        # namespace into the cluster.
        try:
            self._api.delete_namespace(handle.namespace)
        except Exception:
            pass
        self._handles.pop(handle.darwin_id, None)
        msg = (
            f"darwin {handle.darwin_id!r} did not become ready within "
            f"{self._ready_timeout:.0f}s in namespace {handle.namespace!r}"
        )
        if last_err is not None:
            msg = f"{msg}: last error: {last_err}"
        raise DriverError(msg)

    @staticmethod
    def _snapshot_from_dict(darwin_id: str, data: dict) -> DarwinSnapshot:
        return DarwinSnapshot(
            darwin_id=darwin_id,
            alive=bool(data.get("alive", False)),
            paused=bool(data.get("paused", False)),
            killed=bool(data.get("killed", False)),
            tick=int(data.get("tick", 0)),
            days_elapsed=float(data.get("days_elapsed", 0.0)),
            balance_usdc=float(data.get("balance_usdc", 0.0)),
            death_reason=data.get("death_reason"),
        )


# -- real adapters over kubernetes-python-client + httpx --------------------

class _KubernetesClientAdapter:
    """Wraps the real ``kubernetes`` package. Constructed lazily so the
    Protocol-only path (tests, dry runs) never imports kubernetes."""

    def __init__(self) -> None:
        try:
            from kubernetes import client, config  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "K8sDarwinDriver requires the [k8s] extra: "
                "`pip install darwin-dna[k8s]`"
            ) from exc
        # Try in-cluster first; fall back to kubeconfig for local dev.
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        self._core = client.CoreV1Api()
        self._apps = client.AppsV1Api()
        self._net = client.NetworkingV1Api()
        self._models = client

    def create_namespace(self, body: dict) -> None:
        self._core.create_namespace(body=body)

    def create_namespaced_config_map(self, namespace: str, body: dict) -> None:
        self._core.create_namespaced_config_map(namespace=namespace, body=body)

    def create_namespaced_secret(self, namespace: str, body: dict) -> None:
        self._core.create_namespaced_secret(namespace=namespace, body=body)

    def create_namespaced_persistent_volume_claim(
        self, namespace: str, body: dict,
    ) -> None:
        self._core.create_namespaced_persistent_volume_claim(
            namespace=namespace, body=body,
        )

    def create_namespaced_service(self, namespace: str, body: dict) -> None:
        self._core.create_namespaced_service(namespace=namespace, body=body)

    def create_namespaced_deployment(self, namespace: str, body: dict) -> None:
        self._apps.create_namespaced_deployment(namespace=namespace, body=body)

    def create_namespaced_network_policy(self, namespace: str, body: dict) -> None:
        self._net.create_namespaced_network_policy(
            namespace=namespace, body=body,
        )

    def delete_namespace(self, name: str) -> None:
        # propagationPolicy=Background returns immediately; the GC
        # collector tears down children. Foreground is safer but blocks
        # the supervisor longer.
        self._core.delete_namespace(name=name, propagation_policy="Background")

    def list_namespaces_by_label(self, label_selector: str) -> list[dict]:
        resp = self._core.list_namespace(label_selector=label_selector)
        return [ns.to_dict() for ns in resp.items]

    def read_namespaced_deployment_status(
        self, name: str, namespace: str,
    ) -> dict:
        d = self._apps.read_namespaced_deployment_status(
            name=name, namespace=namespace,
        )
        return d.to_dict()


class _HttpxControlClient:
    """Real HTTP RPC client. Uses an httpx.AsyncClient internally."""

    def __init__(self, *, timeout_seconds: float = 5.0) -> None:
        try:
            import httpx  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "K8sDarwinDriver needs httpx (part of the [platform] extra)"
            ) from exc
        self._httpx = httpx
        self._timeout = float(timeout_seconds)

    def _client(self):
        return self._httpx.AsyncClient(timeout=self._timeout)

    async def health(self, base_url: str, token: str) -> bool:
        try:
            async with self._client() as c:
                resp = await c.get(
                    f"{base_url}/healthz",
                    headers={"Authorization": f"Bearer {token}"},
                )
            return resp.status_code == 200
        except Exception:
            return False

    async def snapshot(self, base_url: str, token: str, darwin_id: str) -> dict:
        async with self._client() as c:
            resp = await c.get(
                f"{base_url}/snapshot",
                headers={"Authorization": f"Bearer {token}"},
            )
        resp.raise_for_status()
        return resp.json()

    async def post(
        self,
        base_url: str,
        token: str,
        path: str,
        body: dict | None = None,
    ) -> dict:
        async with self._client() as c:
            resp = await c.post(
                f"{base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=body or {},
            )
        resp.raise_for_status()
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return {}


def make_default_k8s_driver(
    *,
    pod_config: DarwinPodConfig | None = None,
    namespace_prefix: str = "darwin-",
    database_url_secret_ref: tuple[str, str] | None = None,
    secret_extras: dict[str, str] | None = None,
    clock: Callable[[], float] = time.time,
) -> K8sDarwinDriver:
    """Construct the production-shape driver using the real adapters.
    Tests should bypass this and pass a fake ``api`` + ``control_client``
    directly to :class:`K8sDarwinDriver`."""
    return K8sDarwinDriver(
        api=_KubernetesClientAdapter(),
        control_client=_HttpxControlClient(),
        pod_config=pod_config,
        namespace_prefix=namespace_prefix,
        clock=clock,
        database_url_secret_ref=database_url_secret_ref,
        secret_extras=secret_extras,
    )


# Re-export for convenience.
__all__ = [
    "K8sDarwinDriver",
    "KubernetesApi",
    "ControlClient",
    "make_default_k8s_driver",
    "namespace_name",
]
