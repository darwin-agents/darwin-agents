"""Policy baseline for the per-Darwin Kubernetes objects the supervisor
generates (src/darwin_platform/k8s/manifests.py, docs 07/08/24).

Unit tests in tests/unit/darwin_platform/k8s cover individual builders;
this is the cluster-admission view across the whole bundle: every
container — including any sidecar added later — runs hardened, the pod
can't reach the API server, and the namespace is default-deny."""

from __future__ import annotations

import pytest

from darwin_platform.k8s.manifests import DarwinPodConfig, build_bundle


@pytest.fixture(scope="module", params=[False, True], ids=["plain", "soma"])
def bundle(request):
    return build_bundle(
        darwin_id="abc123", namespace_prefix="darwin-", opening_usdc=1.0,
        escalation_auto=None, parent_id=None, manifest_json=None,
        layer_blobs={}, layer_names=[], control_token="tok",
        config=DarwinPodConfig(), soma_enabled=request.param,
    )


def _pod_spec(bundle):
    return bundle.deployment["spec"]["template"]["spec"]


def _containers(bundle):
    spec = _pod_spec(bundle)
    return spec.get("initContainers", []) + spec["containers"]


def test_every_object_lands_in_the_darwins_namespace(bundle):
    ns = bundle.namespace["metadata"]["name"]
    assert ns == "darwin-abc123"
    for obj in (bundle.configmap, bundle.secret, bundle.pvc, bundle.service,
                bundle.deployment, bundle.network_policy, bundle.soma_pvc):
        if obj is not None:
            assert obj["metadata"]["namespace"] == ns, obj["kind"]


def test_pod_is_non_root_with_a_seccomp_profile_and_no_api_token(bundle):
    spec = _pod_spec(bundle)
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"]["type"] in ("RuntimeDefault", "Localhost")
    assert spec.get("runtimeClassName") == "kata", "default sandboxed runtime (doc 07)"
    assert not spec.get("hostNetwork") and not spec.get("hostPID") and not spec.get("hostIPC")


def test_every_container_is_hardened_and_bounded(bundle):
    for c in _containers(bundle):
        sc = c.get("securityContext", {})
        assert sc.get("allowPrivilegeEscalation") is False, c["name"]
        assert not sc.get("privileged"), c["name"]
        assert sc.get("capabilities", {}).get("drop") == ["ALL"], c["name"]
        limits = c.get("resources", {}).get("limits", {})
        assert limits.get("cpu") and limits.get("memory"), f"{c['name']} has no resource limits"


def test_secrets_reach_the_pod_by_reference_only(bundle):
    secret_values = {v for v in bundle.secret.get("data", {}).values()}
    for c in _containers(bundle):
        for env in c.get("env", []):
            assert env.get("value") not in secret_values, f"{env['name']} inlines a secret"
            if "TOKEN" in env["name"] or "KEY" in env["name"]:
                assert "valueFrom" in env, f"{env['name']} should come from a secretKeyRef"


def test_network_policy_is_default_deny_both_ways(bundle):
    policy = bundle.network_policy
    assert policy["kind"] == "NetworkPolicy"
    # The selector must cover the orchestrator pod the Deployment creates.
    selector = policy["spec"]["podSelector"].get("matchLabels", {})
    pod_labels = bundle.deployment["spec"]["template"]["metadata"]["labels"]
    assert selector.items() <= pod_labels.items(), "the policy doesn't select the orchestrator pod"
    assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    # Ingress only from the supervisor (control RPC) — never the open world.
    for rule in policy["spec"]["ingress"]:
        assert rule.get("from"), "an ingress rule without peers admits everyone"
