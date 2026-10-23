"""Shape-tests for the per-Darwin K8s manifest builders.

These are deliberately structural — they assert the shape of the
manifests the supervisor will hand to the kubernetes API. Validation
against a live cluster lives outside this fast suite.
"""

from __future__ import annotations

import json


from darwin_platform.k8s.manifests import (
    CONTROL_API_PORT,
    NAMESPACE_LABEL_DARWIN_ID,
    NAMESPACE_LABEL_MANAGED_BY,
    POD_LABEL_COMPONENT,
    POD_LABEL_DARWIN_ID,
    LITELLM_VIRTUAL_KEY_SECRET_KEY,
    ORGANISM_DB_DSN_SECRET_KEY,
    VAULT_APPROLE_ROLE_ID_SECRET_KEY,
    VAULT_APPROLE_SECRET_ID_SECRET_KEY,
    DarwinPodConfig,
    build_bundle,
    build_configmap,
    build_deployment,
    build_namespace,
    build_network_policy,
    build_pvc,
    build_secret,
    build_service,
    namespace_name,
)


def test_litellm_virtual_key_injected_as_master_key_env():
    """When the supervisor seals a per-Darwin virtual key into the Secret
    (doc 13), the deployment surfaces it as DARWIN_LITELLM_MASTER_KEY via a
    secretKeyRef — so the pod authenticates as its own key, not a shared one."""
    d = build_deployment(
        darwin_id="abc123", namespace="darwin-abc123",
        config=DarwinPodConfig(), opening_usdc=1.0, escalation_auto="approve",
        litellm_virtual_key=True,
    )
    env = d["spec"]["template"]["spec"]["containers"][0]["env"]
    by_name = {e["name"]: e for e in env}
    ref = by_name["DARWIN_LITELLM_MASTER_KEY"]["valueFrom"]["secretKeyRef"]
    assert ref == {
        "name": "darwin-control", "key": LITELLM_VIRTUAL_KEY_SECRET_KEY,
    }


def test_no_virtual_key_env_when_disabled():
    d = build_deployment(
        darwin_id="abc123", namespace="darwin-abc123",
        config=DarwinPodConfig(), opening_usdc=1.0, escalation_auto="approve",
    )
    env = d["spec"]["template"]["spec"]["containers"][0]["env"]
    assert all(e["name"] != "DARWIN_LITELLM_MASTER_KEY" for e in env)


def test_platform_money_rails_emitted_as_env_when_set():
    """The supervisor's forwarded platform money rails reach the pod as
    DARWIN_PLATFORM_MONEY_RAILS so the orchestrator can fold them into its
    money-capabilities manifest."""
    rails = '{"fiat": {"provider": "stripe", "live": true}}'
    d = build_deployment(
        darwin_id="abc123", namespace="darwin-abc123",
        config=DarwinPodConfig(platform_money_rails=rails),
        opening_usdc=1.0, escalation_auto="approve",
    )
    by_name = {
        e["name"]: e
        for e in d["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert by_name["DARWIN_PLATFORM_MONEY_RAILS"]["value"] == rails


def test_no_platform_money_rails_env_by_default():
    d = build_deployment(
        darwin_id="abc123", namespace="darwin-abc123",
        config=DarwinPodConfig(), opening_usdc=1.0, escalation_auto="approve",
    )
    names = {e["name"] for e in d["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "DARWIN_PLATFORM_MONEY_RAILS" not in names


def test_build_bundle_seals_virtual_key_into_secret_and_env():
    bundle = build_bundle(
        darwin_id="abc123", namespace_prefix="darwin-", opening_usdc=1.0,
        escalation_auto="approve", parent_id=None, manifest_json=None,
        layer_blobs={}, layer_names=[], control_token="tok",
        config=DarwinPodConfig(),
        secret_extras={LITELLM_VIRTUAL_KEY_SECRET_KEY: "sk-darwin-9"},
    )
    # The key is in the Secret (base64) ...
    import base64
    raw = bundle.secret["data"][LITELLM_VIRTUAL_KEY_SECRET_KEY]
    assert base64.b64decode(raw).decode() == "sk-darwin-9"
    # ... and the deployment references it as the master-key env.
    env = bundle.deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    by_name = {e["name"]: e for e in env}
    assert "valueFrom" in by_name["DARWIN_LITELLM_MASTER_KEY"]


def test_soma_enabled_mounts_persistent_volume_and_arg():
    """With soma-merge enabled the pod gets the --soma-root arg + a writable
    soma volume mounted; by default that volume is a PVC so promoted self-mods
    survive a pod restart (docs 03/21/05)."""
    from darwin_platform.k8s.manifests import SOMA_MOUNT_PATH

    d = build_deployment(
        darwin_id="abc123", namespace="darwin-abc123",
        config=DarwinPodConfig(), opening_usdc=1.0, escalation_auto="approve",
        soma_enabled=True,
    )
    spec = d["spec"]["template"]["spec"]
    orch = spec["containers"][0]
    assert ["--soma-root", SOMA_MOUNT_PATH] == orch["args"][-2:]
    assert any(
        m["name"] == "darwin-soma" and m["mountPath"] == SOMA_MOUNT_PATH
        for m in orch["volumeMounts"]
    )
    soma_vol = next(v for v in spec["volumes"] if v["name"] == "darwin-soma")
    assert soma_vol["persistentVolumeClaim"]["claimName"] == "darwin-soma"


def test_soma_falls_back_to_emptydir_when_storage_disabled():
    # No storage provisioner (e.g. local/kind): opt into ephemeral soma.
    cfg = DarwinPodConfig(soma_storage_size="")
    d = build_deployment(
        darwin_id="abc123", namespace="darwin-abc123",
        config=cfg, opening_usdc=1.0, escalation_auto="approve",
        soma_enabled=True,
    )
    spec = d["spec"]["template"]["spec"]
    soma_vol = next(v for v in spec["volumes"] if v["name"] == "darwin-soma")
    assert "emptyDir" in soma_vol


def test_no_soma_volume_or_arg_by_default():
    d = build_deployment(
        darwin_id="abc123", namespace="darwin-abc123",
        config=DarwinPodConfig(), opening_usdc=1.0, escalation_auto="approve",
    )
    spec = d["spec"]["template"]["spec"]
    assert "--soma-root" not in spec["containers"][0]["args"]
    assert all(v["name"] != "darwin-soma" for v in spec["volumes"])


def test_build_bundle_threads_soma_enabled_and_builds_pvc():
    bundle = build_bundle(
        darwin_id="abc123", namespace_prefix="darwin-", opening_usdc=1.0,
        escalation_auto="approve", parent_id=None, manifest_json=None,
        layer_blobs={}, layer_names=[], control_token="tok",
        config=DarwinPodConfig(), soma_enabled=True,
    )
    args = bundle.deployment["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--soma-root" in args
    # The persistent soma PVC is part of the bundle.
    assert bundle.soma_pvc is not None
    assert bundle.soma_pvc["metadata"]["name"] == "darwin-soma"
    assert bundle.soma_pvc["spec"]["accessModes"] == ["ReadWriteOnce"]


def test_build_bundle_no_soma_pvc_when_disabled_or_ephemeral():
    # soma off → no PVC.
    off = build_bundle(
        darwin_id="a", namespace_prefix="darwin-", opening_usdc=1.0,
        escalation_auto="approve", parent_id=None, manifest_json=None,
        layer_blobs={}, layer_names=[], control_token="t",
        config=DarwinPodConfig(),
    )
    assert off.soma_pvc is None
    # soma on but storage disabled (emptyDir) → no PVC.
    ephemeral = build_bundle(
        darwin_id="a", namespace_prefix="darwin-", opening_usdc=1.0,
        escalation_auto="approve", parent_id=None, manifest_json=None,
        layer_blobs={}, layer_names=[], control_token="t",
        config=DarwinPodConfig(soma_storage_size=""), soma_enabled=True,
    )
    assert ephemeral.soma_pvc is None


def test_vault_approle_injected_as_role_and_secret_id_env():
    """With tenant namespaces, the per-Darwin AppRole creds are surfaced under
    the exact env names _build_real_factories reads (auto-selecting approle),
    so the pod authenticates to Vault as its own scoped identity (doc 22)."""
    d = build_deployment(
        darwin_id="abc123", namespace="darwin-abc123",
        config=DarwinPodConfig(), opening_usdc=1.0, escalation_auto="approve",
        vault_approle=True,
    )
    env = d["spec"]["template"]["spec"]["containers"][0]["env"]
    by_name = {e["name"]: e for e in env}
    assert by_name["DARWIN_VAULT_ROLE_ID"]["valueFrom"]["secretKeyRef"] == {
        "name": "darwin-control", "key": VAULT_APPROLE_ROLE_ID_SECRET_KEY,
    }
    assert by_name["DARWIN_VAULT_SECRET_ID"]["valueFrom"]["secretKeyRef"] == {
        "name": "darwin-control", "key": VAULT_APPROLE_SECRET_ID_SECRET_KEY,
    }


def test_organism_db_dsn_injected_when_present():
    d = build_deployment(
        darwin_id="abc123", namespace="darwin-abc123",
        config=DarwinPodConfig(), opening_usdc=1.0, escalation_auto="approve",
        organism_db_dsn=True,
    )
    by_name = {e["name"]: e
               for e in d["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert by_name["DARWIN_ORGANISM_DB_DSN"]["valueFrom"]["secretKeyRef"] == {
        "name": "darwin-control", "key": ORGANISM_DB_DSN_SECRET_KEY,
    }


def test_no_vault_approle_env_by_default():
    d = build_deployment(
        darwin_id="abc123", namespace="darwin-abc123",
        config=DarwinPodConfig(), opening_usdc=1.0, escalation_auto="approve",
    )
    names = {e["name"] for e in d["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "DARWIN_VAULT_ROLE_ID" not in names
    assert "DARWIN_VAULT_SECRET_ID" not in names
    assert "DARWIN_ORGANISM_DB_DSN" not in names


def test_build_bundle_seals_approle_and_dsn_into_secret_and_env():
    import base64

    bundle = build_bundle(
        darwin_id="abc123", namespace_prefix="darwin-", opening_usdc=1.0,
        escalation_auto="approve", parent_id=None, manifest_json=None,
        layer_blobs={}, layer_names=[], control_token="tok",
        config=DarwinPodConfig(),
        secret_extras={
            VAULT_APPROLE_ROLE_ID_SECRET_KEY: "role-xyz",
            VAULT_APPROLE_SECRET_ID_SECRET_KEY: "secret-xyz",
            ORGANISM_DB_DSN_SECRET_KEY: "postgresql://role:pw@db/organism_x",
        },
    )
    data = bundle.secret["data"]
    assert base64.b64decode(data[VAULT_APPROLE_SECRET_ID_SECRET_KEY]).decode() == "secret-xyz"
    by_name = {e["name"]: e
               for e in bundle.deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "valueFrom" in by_name["DARWIN_VAULT_ROLE_ID"]
    assert "valueFrom" in by_name["DARWIN_ORGANISM_DB_DSN"]


DARWIN_ID = "abcdef0123456789"
NS = namespace_name("darwin-", DARWIN_ID)


def _config():
    return DarwinPodConfig(
        orchestrator_image="myrepo/darwin:1.0",
        runtime_class_name="kata",
        platform_data_namespace="platform-data",
        postgres_namespace="data",
        postgres_pod_label_key="app",
        postgres_pod_label_value="postgres",
    )


def test_namespace_name_is_prefix_bounded_and_lowercased():
    assert namespace_name("darwin-", "ABCDEF") == "darwin-abcdef"
    long_id = "x" * 200
    assert len(namespace_name("darwin-", long_id)) <= 63


def test_namespace_has_required_labels_and_annotations():
    ns = build_namespace(
        darwin_id=DARWIN_ID, namespace_prefix="darwin-",
        parent_id="parentX", opening_usdc=42.5, spawned_at=1700.0,
    )
    labels = ns["metadata"]["labels"]
    assert labels[NAMESPACE_LABEL_MANAGED_BY] == "darwin-platform"
    assert labels[NAMESPACE_LABEL_DARWIN_ID] == DARWIN_ID
    # Pod security baseline must be enforced — restricted is what doc-08
    # production wants for per-Darwin pods.
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    annotations = ns["metadata"]["annotations"]
    assert annotations["darwin.io/parent-id"] == "parentX"
    assert annotations["darwin.io/opening-usdc"] == "42.5"


def test_configmap_carries_manifest_and_layer_blobs():
    cm = build_configmap(
        darwin_id=DARWIN_ID, namespace=NS,
        manifest_json='{"identity":"x"}',
        layer_blobs={"00-zero": b"genome", "01-amend": b"\x00\xff"},
        layer_names=["00-zero", "01-amend"],
    )
    assert cm["data"]["manifest.json"] == '{"identity":"x"}'
    assert json.loads(cm["data"]["layer_names.json"]) == ["00-zero", "01-amend"]
    assert set(cm["binaryData"]) == {"layer-00-zero", "layer-01-amend"}
    meta = json.loads(cm["data"]["meta.json"])
    assert meta["darwin_id"] == DARWIN_ID


def test_secret_base64_encodes_token():
    import base64
    s = build_secret(
        darwin_id=DARWIN_ID, namespace=NS, control_token="tok-abc",
        extra={"OPENAI_API_KEY": "sk-xx"},
    )
    assert s["type"] == "Opaque"
    decoded = base64.b64decode(s["data"]["control_token"]).decode()
    assert decoded == "tok-abc"
    assert base64.b64decode(s["data"]["OPENAI_API_KEY"]).decode() == "sk-xx"


def test_pvc_uses_storage_class_when_set():
    cfg = DarwinPodConfig(data_storage_class="ssd-fast", data_storage_size="2Gi")
    pvc = build_pvc(darwin_id=DARWIN_ID, namespace=NS, config=cfg)
    assert pvc["spec"]["storageClassName"] == "ssd-fast"
    assert pvc["spec"]["resources"]["requests"]["storage"] == "2Gi"


def test_service_selects_orchestrator_pods():
    svc = build_service(darwin_id=DARWIN_ID, namespace=NS)
    selector = svc["spec"]["selector"]
    assert selector[POD_LABEL_DARWIN_ID] == DARWIN_ID
    assert selector[POD_LABEL_COMPONENT] == "orchestrator"
    assert svc["spec"]["ports"][0]["port"] == CONTROL_API_PORT


def test_deployment_runs_orchestrator_under_kata_with_durable_data():
    cfg = _config()
    d = build_deployment(
        darwin_id=DARWIN_ID, namespace=NS, config=cfg,
        opening_usdc=100.0, escalation_auto="approve",
    )
    pod = d["spec"]["template"]["spec"]
    assert pod["runtimeClassName"] == "kata"
    assert [c["name"] for c in pod["containers"]] == ["orchestrator"]
    # DNA ConfigMap, control Secret, and the PVC-backed /data (audit log,
    # checkpoint, wallet feed survive a pod restart).
    mounts = {m["name"]: m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
    assert {"darwin-dna", "darwin-control"} <= set(mounts)
    assert mounts["darwin-data"] == "/data"
    vols = {v["name"]: v for v in pod["volumes"]}
    assert vols["darwin-data"]["persistentVolumeClaim"]["claimName"] == "darwin-data"
    # CLI args carry the per-Darwin opening_usdc + token path.
    args = pod["containers"][0]["args"]
    assert "--darwin-id" in args and DARWIN_ID in args
    assert "--control-port" in args and str(CONTROL_API_PORT) in args
    # Non-root, dropped caps.
    sc = pod["containers"][0]["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert sc["capabilities"]["drop"] == ["ALL"]


def test_deployment_forwards_heartbeat():
    """A pod defaulting to a 5ms heartbeat would make ~200 paid LLM calls/s."""
    d = build_deployment(
        darwin_id=DARWIN_ID, namespace=NS,
        config=DarwinPodConfig(heartbeat_seconds=7.5),
        opening_usdc=100.0, escalation_auto=None,
    )
    args = d["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args[args.index("--heartbeat-seconds") + 1] == "7.5"
    assert args[args.index("--escalation-auto") + 1] == "none"


def test_pod_args_overlay_substrate_on_full_serve_namespace(tmp_path):
    """The pod builds its substrate from a full ``serve`` namespace overlaid
    with the supervisor-forwarded settings — every flag present."""
    import json

    from darwin_platform.runtime.orchestrator import _pod_args

    path = tmp_path / "substrate.json"
    path.write_text(json.dumps({
        "wallet_provider": "watch", "wallet_address": "0x" + "a" * 40,
        "network": "mainnet", "e2b_api_key": "e2b-x",
    }))
    ns = _pod_args(path)
    assert ns.wallet_provider == "watch"
    assert ns.network == "mainnet"
    assert ns.e2b_api_key == "e2b-x"
    assert ns.cdp_api_key_id is None  # pods never get signing credentials
    assert _pod_args(tmp_path / "absent.json").wallet_provider is None


def test_deployment_threads_database_url_secret_ref():
    cfg = _config()
    d = build_deployment(
        darwin_id=DARWIN_ID, namespace=NS, config=cfg,
        opening_usdc=100.0, escalation_auto="approve",
        database_url_secret_ref=("darwin-pg", "DATABASE_URL"),
    )
    envs = d["spec"]["template"]["spec"]["containers"][0]["env"]
    db_env = next(e for e in envs if e["name"] == "DATABASE_URL")
    assert db_env["valueFrom"]["secretKeyRef"]["name"] == "darwin-pg"


def test_network_policy_denies_by_default_with_pinholes():
    cfg = _config()
    np = build_network_policy(darwin_id=DARWIN_ID, namespace=NS, config=cfg)
    spec = np["spec"]
    assert spec["policyTypes"] == ["Ingress", "Egress"]
    # Ingress: supervisor → orchestrator control port only.
    ingress = spec["ingress"]
    assert len(ingress) == 1
    assert ingress[0]["ports"][0]["port"] == CONTROL_API_PORT
    # Egress: DNS, egress-proxy, Postgres (because cfg has it set).
    egress_to = [r["to"] for r in spec["egress"]]
    flat_namespaces = [
        peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
        for to_list in egress_to for peer in to_list
        if "namespaceSelector" in peer
    ]
    assert "kube-system" in flat_namespaces
    assert "platform-data" in flat_namespaces
    assert "data" in flat_namespaces


def test_network_policy_omits_postgres_when_unconfigured():
    cfg = DarwinPodConfig(platform_data_namespace="platform-data")  # no postgres
    np = build_network_policy(darwin_id=DARWIN_ID, namespace=NS, config=cfg)
    egress_namespaces = [
        peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
        for rule in np["spec"]["egress"] for peer in rule["to"]
        if "namespaceSelector" in peer
    ]
    assert "kube-system" in egress_namespaces
    assert "platform-data" in egress_namespaces
    # Postgres rule was not appended.
    assert "data" not in egress_namespaces


def test_build_bundle_returns_every_resource_in_one_namespace():
    bundle = build_bundle(
        darwin_id=DARWIN_ID,
        namespace_prefix="darwin-",
        opening_usdc=100.0,
        escalation_auto="approve",
        parent_id="parentX",
        manifest_json='{}',
        layer_blobs={"00-zero": b"x"},
        layer_names=["00-zero"],
        control_token="tok-1",
        config=_config(),
    )
    ns_name = bundle.namespace["metadata"]["name"]
    for obj in (bundle.configmap, bundle.secret, bundle.pvc, bundle.service,
                bundle.deployment, bundle.network_policy):
        assert obj["metadata"]["namespace"] == ns_name


def test_extra_env_lands_on_orchestrator_container():
    """`DarwinPodConfig.extra_env` is the seam the supervisor uses to
    forward substrate config (DARWIN_LITELLM_URL, DARWIN_VAULT_URL, ...)
    onto per-Darwin pods in --driver=k8s mode. Each entry must appear in
    the orchestrator container's env list, in addition to the built-ins
    DARWIN_ID / DARWIN_NAMESPACE that build_deployment always emits."""
    cfg = DarwinPodConfig(
        extra_env=(
            {"name": "DARWIN_LITELLM_URL", "value": "http://litellm:4000"},
            {"name": "DARWIN_LITELLM_MASTER_KEY", "value": "sk-test"},
            {"name": "DARWIN_VAULT_URL", "value": "http://vault:8200"},
            {"name": "DARWIN_VAULT_TOKEN", "value": "root-tok"},
        ),
    )
    d = build_deployment(
        darwin_id="abc123", namespace="darwin-abc123",
        config=cfg, opening_usdc=1.0, escalation_auto="approve",
    )
    env = d["spec"]["template"]["spec"]["containers"][0]["env"]
    env_names = {e["name"]: e.get("value") for e in env if "value" in e}
    assert env_names["DARWIN_LITELLM_URL"] == "http://litellm:4000"
    assert env_names["DARWIN_LITELLM_MASTER_KEY"] == "sk-test"
    assert env_names["DARWIN_VAULT_URL"] == "http://vault:8200"
    assert env_names["DARWIN_VAULT_TOKEN"] == "root-tok"
    # The built-ins also still present.
    assert env_names["DARWIN_ID"] == "abc123"
    assert env_names["DARWIN_NAMESPACE"] == "darwin-abc123"


def test_network_policy_without_proxy_allows_public_https_and_llm_only():
    """No egress proxy (the Hetzner default): pods may reach LiteLLM in its
    namespace and public HTTPS — never cluster-private or metadata ranges."""
    cfg = DarwinPodConfig(platform_data_namespace="", llm_namespace="litellm")
    egress = build_network_policy(
        darwin_id=DARWIN_ID, namespace=NS, config=cfg,
    )["spec"]["egress"]
    blocks = [p["ipBlock"] for r in egress for p in r["to"] if "ipBlock" in p]
    assert blocks == [{
        "cidr": "0.0.0.0/0",
        "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                   "169.254.0.0/16", "100.64.0.0/10"],
    }]
    https = next(r for r in egress if any("ipBlock" in p for p in r["to"]))
    assert https["ports"] == [{"protocol": "TCP", "port": 443}]
    llm = next(
        r for r in egress for p in r["to"]
        if p.get("namespaceSelector", {}).get("matchLabels", {}).get(
            "kubernetes.io/metadata.name") == "litellm"
    )
    assert llm["ports"] == [{"protocol": "TCP", "port": 4000}]
    assert "platform-data" not in str(egress)
