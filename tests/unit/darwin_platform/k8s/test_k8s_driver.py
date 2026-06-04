"""Drive-loop + RPC tests for K8sDarwinDriver against an in-memory
fake kubernetes API + control client. Real cluster calls live outside
this fast suite."""

from __future__ import annotations


import pytest

from darwin_platform.runtime.driver import DarwinNotFound, DarwinSnapshot, DarwinSpec, DriverError
from darwin_platform.k8s.driver import K8sDarwinDriver
from darwin_platform.k8s.manifests import DarwinPodConfig


class _RecordingApi:
    """Captures every call the driver makes so the test can assert
    ordering and contents."""

    def __init__(
        self,
        *,
        fail_on: set[str] | None = None,
        namespaces: list[dict] | None = None,
        secrets: dict[tuple[str, str], dict] | None = None,
    ):
        self.created: list[tuple[str, str, dict]] = []  # (kind, ns, body)
        self.deleted_namespaces: list[str] = []
        self._fail_on = fail_on or set()
        self._namespaces = namespaces or []
        self._secrets = secrets or {}

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
        return list(self._namespaces)

    def read_namespaced_secret(self, name, namespace):
        try:
            return self._secrets[(name, namespace)]
        except KeyError:
            raise RuntimeError(f"secret {name} not found in {namespace}")

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

    async def get(self, base_url, token, path):
        self.calls.append((path, base_url))
        if path == "/events":
            return {"records": [
                {"seq": 1, "ts": 1.0, "kind": "event:test",
                 "darwin_id": "d-1", "payload": {}},
            ]}
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
async def test_start_merges_per_spawn_secret_extras_into_secret():
    """The supervisor passes the minted LiteLLM virtual key via
    DarwinSpec.secret_extras; the driver must merge it into the per-Darwin
    Secret so the pod can read it (doc 13)."""
    import base64
    from darwin_platform.k8s.manifests import LITELLM_VIRTUAL_KEY_SECRET_KEY

    api = _RecordingApi()
    control = _RecordingControl(become_ready_after=1)
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        ready_poll_interval_seconds=0.0, ready_timeout_seconds=2.0,
    )
    await driver.start(DarwinSpec(
        darwin_id="d-vk", opening_usdc=50.0,
        secret_extras={LITELLM_VIRTUAL_KEY_SECRET_KEY: "sk-darwin-7"},
    ))
    secret_body = next(b for (k, _ns, b) in api.created if k == "secret")
    raw = secret_body["data"][LITELLM_VIRTUAL_KEY_SECRET_KEY]
    assert base64.b64decode(raw).decode() == "sk-darwin-7"


@pytest.mark.asyncio
async def test_snapshot_parses_llm_spend_usdc():
    api = _RecordingApi()

    class _SpendControl(_RecordingControl):
        async def snapshot(self, base_url, token, darwin_id):
            return {
                "darwin_id": darwin_id, "alive": True, "paused": False,
                "killed": False, "tick": 1, "days_elapsed": 0.1,
                "balance_usdc": 100.0, "llm_spend_usdc": 3.25,
                "merkle_root": "deadbeef",
            }

    driver = K8sDarwinDriver(
        api=api, control_client=_SpendControl(become_ready_after=1),
        ready_poll_interval_seconds=0.0, ready_timeout_seconds=2.0,
    )
    snap = await driver.start(DarwinSpec(darwin_id="d-s", opening_usdc=50.0))
    assert snap.llm_spend_usdc == 3.25
    assert snap.merkle_root == "deadbeef"


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
async def test_soma_enabled_spec_creates_persistent_soma_pvc():
    """A soma-enabled spawn provisions the per-Darwin soma PVC (alongside the
    lancedb PVC) so promoted self-mods survive a pod restart."""
    api = _RecordingApi()
    control = _RecordingControl(become_ready_after=1)
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        ready_poll_interval_seconds=0.0, ready_timeout_seconds=2.0,
    )
    await driver.start(DarwinSpec(
        darwin_id="d-soma", opening_usdc=50.0, soma_enabled=True,
    ))
    pvc_names = [
        b["metadata"]["name"] for (k, _ns, b) in api.created if k == "pvc"
    ]
    assert "darwin-soma" in pvc_names
    assert "darwin-lancedb" in pvc_names


@pytest.mark.asyncio
async def test_soma_disabled_spec_creates_no_soma_pvc():
    api = _RecordingApi()
    control = _RecordingControl(become_ready_after=1)
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        ready_poll_interval_seconds=0.0, ready_timeout_seconds=2.0,
    )
    await driver.start(DarwinSpec(darwin_id="d-nosoma", opening_usdc=50.0))
    pvc_names = [
        b["metadata"]["name"] for (k, _ns, b) in api.created if k == "pvc"
    ]
    assert "darwin-soma" not in pvc_names


@pytest.mark.asyncio
async def test_forensics_reads_pod_control_endpoint():
    api = _RecordingApi()

    class _ForensicsControl(_RecordingControl):
        async def get(self, base_url, token, path):
            self.calls.append((path, base_url))
            if path == "/forensics":
                return {
                    "schema": "darwin-platform.soma-forensic-k8s/1",
                    "snapshot_id": "death-1000", "darwin_id": "d-f",
                    "soma_digest": "abc", "file_count": 2,
                }
            return {}

    control = _ForensicsControl(become_ready_after=1)
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        ready_poll_interval_seconds=0.0, ready_timeout_seconds=2.0,
    )
    await driver.start(DarwinSpec(darwin_id="d-f", opening_usdc=50.0))
    data = await driver.forensics("d-f")
    assert data["snapshot_id"] == "death-1000"
    assert data["soma_digest"] == "abc"
    assert "/forensics" in [p for (p, _) in control.calls]


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


# ---------------------------------------------------------------------------
# --wallet-provider forwarding to per-Darwin orchestrator pods
# ---------------------------------------------------------------------------


def _orchestrator_args(api_create: list) -> list[str]:
    """Pull the orchestrator container's args list out of the deployment
    body the driver submitted to the K8s API."""
    deployments = [body for kind, _, body in api_create if kind == "deployment"]
    assert deployments, "no deployment was created"
    pod_spec = deployments[0]["spec"]["template"]["spec"]
    orchestrator = next(
        c for c in pod_spec["containers"] if c["name"] == "orchestrator"
    )
    return orchestrator["args"]


@pytest.mark.asyncio
async def test_k8s_spawn_forwards_default_wallet_provider_cdp():
    """Default DarwinPodConfig.wallet_provider is "cdp" — the supervisor's
    default CLI flag. A K8s spawn produces a deployment whose orchestrator
    args carry --wallet-provider cdp."""
    api = _RecordingApi()
    control = _RecordingControl(become_ready_after=1)
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        pod_config=DarwinPodConfig(),
    )
    await driver.start(DarwinSpec(
        darwin_id="d-abc", opening_usdc=100.0, escalation_auto="approve",
    ))
    args = _orchestrator_args(api.created)
    assert "--wallet-provider" in args
    assert args[args.index("--wallet-provider") + 1] == "cdp"


@pytest.mark.asyncio
async def test_k8s_spawn_forwards_local_vault_wallet_provider():
    """When the operator configures wallet_provider="local-vault", every
    spawn delivers --wallet-provider local-vault to the per-Darwin pod."""
    api = _RecordingApi()
    control = _RecordingControl(become_ready_after=1)
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        pod_config=DarwinPodConfig(wallet_provider="local-vault"),
    )
    await driver.start(DarwinSpec(
        darwin_id="d-xyz", opening_usdc=100.0, escalation_auto="approve",
    ))
    args = _orchestrator_args(api.created)
    assert args[args.index("--wallet-provider") + 1] == "local-vault"


@pytest.mark.asyncio
async def test_k8s_spawn_forwards_web3_wallet_provider():
    """The testnet-only shared-key path also propagates end-to-end."""
    api = _RecordingApi()
    control = _RecordingControl(become_ready_after=1)
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        pod_config=DarwinPodConfig(wallet_provider="web3"),
    )
    await driver.start(DarwinSpec(
        darwin_id="d-test", opening_usdc=100.0, escalation_auto="approve",
    ))
    args = _orchestrator_args(api.created)
    assert args[args.index("--wallet-provider") + 1] == "web3"


@pytest.mark.asyncio
async def test_k8s_spawn_consistent_wallet_provider_across_darwins():
    """A driver configured with one wallet_provider delivers it to every
    Darwin it spawns — no per-spawn drift."""
    api = _RecordingApi()
    control = _RecordingControl(become_ready_after=1)
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        pod_config=DarwinPodConfig(wallet_provider="local-vault"),
    )
    await driver.start(DarwinSpec(
        darwin_id="d-1", opening_usdc=100.0, escalation_auto="approve",
    ))
    await driver.start(DarwinSpec(
        darwin_id="d-2", opening_usdc=100.0, escalation_auto="approve",
    ))
    deployments = [body for kind, _, body in api.created if kind == "deployment"]
    assert len(deployments) == 2
    for body in deployments:
        pod_spec = body["spec"]["template"]["spec"]
        orchestrator = next(
            c for c in pod_spec["containers"] if c["name"] == "orchestrator"
        )
        args = orchestrator["args"]
        assert args[args.index("--wallet-provider") + 1] == "local-vault"


@pytest.mark.asyncio
async def test_reconcile_readopts_existing_namespaces():
    """After a restart the driver's in-memory handles are gone. reconcile()
    lists the darwin-* namespaces, reads each control token back from its
    Secret, and rebuilds a usable handle (snapshot routes again)."""
    import base64

    ns = {
        "metadata": {
            "name": "darwin-d-9",
            "labels": {
                "darwin.io/managed-by": "darwin-platform",
                "darwin.io/darwin-id": "d-9",
            },
            "annotations": {
                "darwin.io/spawned-at": "1700.5",
                "darwin.io/parent-id": "d-parent",
                "darwin.io/opening-usdc": "42.0",
            },
        },
    }
    secret = {"data": {"control_token": base64.b64encode(b"tok-9").decode("ascii")}}
    api = _RecordingApi(
        namespaces=[ns],
        secrets={("darwin-control", "darwin-d-9"): secret},
    )
    control = _RecordingControl(become_ready_after=1)
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        ready_poll_interval_seconds=0.0, ready_timeout_seconds=2.0,
    )

    adopted = await driver.reconcile()
    assert len(adopted) == 1
    assert adopted[0]["darwin_id"] == "d-9"
    assert adopted[0]["parent_id"] == "d-parent"
    assert adopted[0]["spawned_at"] == 1700.5
    assert adopted[0]["opening_usdc"] == 42.0

    # The rebuilt handle is usable — snapshot routes through the control client.
    snap = await driver.snapshot("d-9")
    assert snap.darwin_id == "d-9"
    assert "d-9" in await driver.list_ids()

    # Idempotent: a second reconcile re-adopts nothing already known.
    assert await driver.reconcile() == []


@pytest.mark.asyncio
async def test_reconcile_skips_namespace_without_token():
    """A namespace whose control Secret can't be read is left orphaned for an
    operator, not half-adopted."""
    ns = {
        "metadata": {
            "name": "darwin-d-x",
            "labels": {
                "darwin.io/managed-by": "darwin-platform",
                "darwin.io/darwin-id": "d-x",
            },
        },
    }
    api = _RecordingApi(namespaces=[ns], secrets={})  # no secret → unreadable
    driver = K8sDarwinDriver(
        api=api, control_client=_RecordingControl(),
        ready_poll_interval_seconds=0.0, ready_timeout_seconds=1.0,
    )
    assert await driver.reconcile() == []
    assert await driver.list_ids() == []


@pytest.mark.asyncio
async def test_events_records_proxies_orchestrator_audit_log():
    """events_records() GETs /events on the orchestrator and returns the
    raw records (supervisor does the filtering/paging)."""
    api = _RecordingApi()
    control = _RecordingControl(become_ready_after=1)
    driver = K8sDarwinDriver(
        api=api, control_client=control,
        ready_poll_interval_seconds=0.0, ready_timeout_seconds=2.0,
    )
    await driver.start(DarwinSpec(darwin_id="d-1", opening_usdc=50.0))
    records = await driver.events_records("d-1")
    assert ("/events" in [p for (p, _b) in control.calls])
    assert records and records[0]["darwin_id"] == "d-1"
