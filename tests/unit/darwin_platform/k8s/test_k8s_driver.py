"""Drive-loop + RPC tests for K8sDarwinDriver against an in-memory
fake kubernetes API + control client. Real cluster calls live outside
this fast suite."""

from __future__ import annotations

import asyncio

import pytest

from darwin_platform.runtime.driver import DarwinNotFound, DarwinSnapshot, DarwinSpec, DriverError
from darwin_platform.k8s.driver import K8sDarwinDriver
from darwin_platform.k8s.manifests import DarwinPodConfig


class _RecordingApi:
    """Captures every call the driver makes so the test can assert
    ordering and contents."""

    def __init__(self, *, fail_on: set[str] | None = None):
        self.created: list[tuple[str, str, dict]] = []  # (kind, ns, body)
        self.deleted_namespaces: list[str] = []
        self._fail_on = fail_on or set()

    def _maybe_fail(self, kind: str):
        if kind in self._fail_on:
            raise RuntimeError(f"injected failure for {kind}")

    def create_namespace(self, body):
        self._maybe_fail("namespace")
        self.created.append(("namespace", body["metadata"]["name"], body))

    def create_namespaced_config_map(self, namespace, body):
        self._maybe_fail("configmap")
        self.created.append(("configmap", namespace, body))

    def create_namespaced_secret(self, namespace, body):
        self._maybe_fail("secret")
        self.created.append(("secret", namespace, body))

    def create_namespaced_persistent_volume_claim(self, namespace, body):
        self._maybe_fail("pvc")
        self.created.append(("pvc", namespace, body))

    def create_namespaced_service(self, namespace, body):
        self._maybe_fail("service")
        self.created.append(("service", namespace, body))

    def create_namespaced_deployment(self, namespace, body):
        self._maybe_fail("deployment")
        self.created.append(("deployment", namespace, body))

    def create_namespaced_network_policy(self, namespace, body):
        self._maybe_fail("networkpolicy")
        self.created.append(("networkpolicy", namespace, body))

    def delete_namespace(self, name):
        self.deleted_namespaces.append(name)

    def list_namespaces_by_label(self, label_selector):
        return []

    def read_namespaced_deployment_status(self, name, namespace):
        return {"status": {"availableReplicas": 1, "readyReplicas": 1}}


class _RecordingControl:
    def __init__(self, *, become_ready_after: int = 1):
        self.calls: list[tuple[str, str]] = []  # (path, base_url)
        self._become_ready_after = become_ready_after
        self._health_calls = 0

    async def health(self, base_url, token):
        self._health_calls += 1
        return self._health_calls >= self._become_ready_after

    async def snapshot(self, base_url, token, darwin_id):
        self.calls.append(("snapshot", base_url))
        return {
            "darwin_id": darwin_id, "alive": True, "paused": False,
            "killed": False, "tick": 7, "days_elapsed": 0.5,
            "balance_usdc": 100.0,
        }

    async def post(self, base_url, token, path, body=None):
        self.calls.append((path, base_url))
        return {}


@pytest.mark.asyncio
async def test_start_creates_every_resource_in_order():
    api = _RecordingApi()
    control = _RecordingControl(become_ready_after=2)
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        pod_config=DarwinPodConfig(runtime_class_name="kata"),
        ready_poll_interval_seconds=0.0,
        ready_timeout_seconds=2.0,
    )
    snap = await driver.start(DarwinSpec(darwin_id="d-1", opening_usdc=50.0))
    kinds = [k for (k, _ns, _body) in api.created]
    # Namespace must come first; otherwise the namespaced resources have
    # nowhere to land.
    assert kinds[0] == "namespace"
    assert set(kinds) == {
        "namespace", "configmap", "secret", "pvc", "service",
        "deployment", "networkpolicy",
    }
    assert snap.alive is True
    assert snap.balance_usdc == 100.0


@pytest.mark.asyncio
async def test_start_rolls_back_namespace_on_failure():
    api = _RecordingApi(fail_on={"deployment"})
    control = _RecordingControl()
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        ready_poll_interval_seconds=0.0, ready_timeout_seconds=0.5,
    )
    with pytest.raises(DriverError):
        await driver.start(DarwinSpec(darwin_id="d-fail", opening_usdc=50.0))
    # The half-created namespace must be deleted so we don't leak.
    assert len(api.deleted_namespaces) == 1


@pytest.mark.asyncio
async def test_pause_resume_kill_route_to_correct_paths():
    api = _RecordingApi()
    control = _RecordingControl()
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        ready_poll_interval_seconds=0.0, ready_timeout_seconds=2.0,
    )
    await driver.start(DarwinSpec(darwin_id="d-2", opening_usdc=50.0))
    await driver.pause("d-2", reason="op")
    await driver.resume("d-2")
    await driver.kill("d-2", reason="bye")
    paths = [p for (p, _) in control.calls]
    assert "/pause" in paths
    assert "/resume" in paths
    assert "/kill" in paths


@pytest.mark.asyncio
async def test_snapshot_uses_control_endpoint():
    api = _RecordingApi()
    control = _RecordingControl()
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        ready_poll_interval_seconds=0.0, ready_timeout_seconds=2.0,
    )
    await driver.start(DarwinSpec(darwin_id="d-3", opening_usdc=50.0))
    snap = await driver.snapshot("d-3")
    assert isinstance(snap, DarwinSnapshot)
    assert snap.alive is True


@pytest.mark.asyncio
async def test_stop_deletes_namespace_and_clears_handle():
    api = _RecordingApi()
    control = _RecordingControl()
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        ready_poll_interval_seconds=0.0, ready_timeout_seconds=2.0,
    )
    await driver.start(DarwinSpec(darwin_id="d-4", opening_usdc=50.0))
    await driver.stop("d-4")
    assert api.deleted_namespaces  # at least one
    with pytest.raises(DarwinNotFound):
        await driver.snapshot("d-4")
    # Idempotent.
    await driver.stop("d-4")


@pytest.mark.asyncio
async def test_start_refuses_duplicate_darwin_id():
    api = _RecordingApi()
    control = _RecordingControl()
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        ready_poll_interval_seconds=0.0, ready_timeout_seconds=2.0,
    )
    await driver.start(DarwinSpec(darwin_id="d-5", opening_usdc=50.0))
    with pytest.raises(DriverError):
        await driver.start(DarwinSpec(darwin_id="d-5", opening_usdc=50.0))
