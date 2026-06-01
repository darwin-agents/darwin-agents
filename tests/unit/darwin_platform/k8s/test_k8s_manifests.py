"""Shape-tests for the per-Darwin K8s manifest builders.

These are deliberately structural — they assert the shape of the
manifests the supervisor will hand to the kubernetes API. Validation
against a live cluster lives outside this fast suite.
"""

from __future__ import annotations

import json


from darwin_platform.k8s.manifests import (
    CONTROL_API_PORT,
    LANCEDB_PORT,
    NAMESPACE_LABEL_DARWIN_ID,
    NAMESPACE_LABEL_MANAGED_BY,
    POD_LABEL_COMPONENT,
    POD_LABEL_DARWIN_ID,
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
    cfg = DarwinPodConfig(lancedb_storage_class="ssd-fast", lancedb_storage_size="2Gi")
    pvc = build_pvc(darwin_id=DARWIN_ID, namespace=NS, config=cfg)
    assert pvc["spec"]["storageClassName"] == "ssd-fast"
    assert pvc["spec"]["resources"]["requests"]["storage"] == "2Gi"


def test_service_selects_orchestrator_pods():
    svc = build_service(darwin_id=DARWIN_ID, namespace=NS)
    selector = svc["spec"]["selector"]
    assert selector[POD_LABEL_DARWIN_ID] == DARWIN_ID
    assert selector[POD_LABEL_COMPONENT] == "orchestrator"
    assert svc["spec"]["ports"][0]["port"] == CONTROL_API_PORT


def test_deployment_runs_orchestrator_under_kata_with_lancedb_sidecar():
    cfg = _config()
    d = build_deployment(
        darwin_id=DARWIN_ID, namespace=NS, config=cfg,
        opening_usdc=100.0, escalation_auto="approve",
    )
    pod = d["spec"]["template"]["spec"]
    assert pod["runtimeClassName"] == "kata"
    names = [c["name"] for c in pod["containers"]]
    assert names == ["orchestrator", "lancedb"]
    # Orchestrator must mount the DNA ConfigMap and the control Secret
    mounts = {m["name"] for m in pod["containers"][0]["volumeMounts"]}
    assert {"darwin-dna", "darwin-control"} <= mounts
    # LanceDB sidecar binds the PVC.
    sidecar = pod["containers"][1]
    assert sidecar["ports"][0]["containerPort"] == LANCEDB_PORT
    assert any(m["name"] == "darwin-lancedb" for m in sidecar["volumeMounts"])
    # CLI args carry the per-Darwin opening_usdc + token path.
    args = pod["containers"][0]["args"]
    assert "--darwin-id" in args and DARWIN_ID in args
    assert "--control-port" in args and str(CONTROL_API_PORT) in args
    # Non-root, dropped caps.
    sc = pod["containers"][0]["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert sc["capabilities"]["drop"] == ["ALL"]


def test_deployment_forwards_wallet_provider_to_orchestrator_args():
    """Step (3): the supervisor's --wallet-provider choice must reach the
    per-Darwin orchestrator pod so each Darwin uses the same backend."""
    cfg = DarwinPodConfig(
        orchestrator_image="myrepo/darwin:1.0",
        runtime_class_name="kata",
        platform_data_namespace="platform-data",
        postgres_namespace="data",
        postgres_pod_label_key="app",
        postgres_pod_label_value="postgres",
        wallet_provider="local-vault",
    )
    d = build_deployment(
        darwin_id=DARWIN_ID, namespace=NS, config=cfg,
        opening_usdc=100.0, escalation_auto="approve",
    )
    args = d["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--wallet-provider" in args
    assert args[args.index("--wallet-provider") + 1] == "local-vault"


def test_deployment_default_wallet_provider_is_cdp():
    """Matches the supervisor CLI default — production-safe out of the box."""
    cfg = _config()
    d = build_deployment(
        darwin_id=DARWIN_ID, namespace=NS, config=cfg,
        opening_usdc=100.0, escalation_auto="approve",
    )
    args = d["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args[args.index("--wallet-provider") + 1] == "cdp"


def test_orchestrator_argparse_accepts_wallet_provider():
    """The orchestrator-pod CLI must accept the flag the manifest forwards."""
    from darwin_platform.runtime.orchestrator import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "--darwin-id", "x",
        "--manifest-path", "/etc/m.json",
        "--layer-blob-dir", "/etc",
        "--layer-names-path", "/etc/n.json",
        "--wallet-provider", "local-vault",
    ])
    assert args.wallet_provider == "local-vault"


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
