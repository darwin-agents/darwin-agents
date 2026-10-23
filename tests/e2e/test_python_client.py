"""E2E: the typed Python SDK (``darwin_platform.client``) against the real
``serve`` process — the shape CI scripts and operator automation use."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from darwin_platform.client import (
    DarwinClient,
    FreelancerRegistration,
    PlatformApiError,
    PreApprovalSpec,
)


@pytest.fixture
def sdk(server_factory, tokens, tmp_path):
    # SQLite persistence: the freelancer registry is backed by it (doc 17).
    srv = server_factory(
        db=str(tmp_path / "state.db"), extra_args=["--auth-tokens", str(tokens.path)],
    ).start()
    with srv.client(tokens.alice) as raw:
        did = raw.post("/darwins", json={"opening_usdc": 80.0}).json()["darwin_id"]

    def connect(token: str) -> DarwinClient:
        return DarwinClient(base_url=srv.base_url, token=token, timeout=5.0)

    with connect(tokens.alice) as alice, connect(tokens.bob) as bob, \
         connect(tokens.operator) as ops:
        yield SimpleNamespace(alice=alice, bob=bob, ops=ops, did=did)


def test_preapproval_lifecycle_is_operator_only(sdk):
    sdk.ops.set_preapproval(sdk.did, "hosting", PreApprovalSpec(max_amount_usdc=5.0, max_uses=3))
    assert any("hosting" in str(rule) for rule in sdk.ops.list_preapprovals(sdk.did))

    with pytest.raises(PlatformApiError) as exc:
        sdk.alice.list_preapprovals(sdk.did)
    assert exc.value.status == 403

    sdk.ops.revoke_preapproval(sdk.did, "hosting")
    assert not any("hosting" in str(rule) for rule in sdk.ops.list_preapprovals(sdk.did))


def test_self_serve_freelancer_registry_is_per_owner(sdk):
    alice, bob = sdk.alice, sdk.bob
    alice.register_my_freelancer(FreelancerRegistration(
        owner_subject="ignored-server-side",
        freelancer_id="upwork:writer-42",
        fiat_destination="acct_test_123",
    ))
    assert [f["freelancer_id"] for f in alice.list_my_freelancers()] == ["upwork:writer-42"]
    # The body's owner_subject is ignored — the token's subject wins, so
    # Bob's registry is untouched.
    assert bob.list_my_freelancers() == []

    alice.revoke_my_freelancer("upwork:writer-42")
    assert alice.list_my_freelancers() == []


def test_errors_carry_the_http_status(sdk):
    with pytest.raises(PlatformApiError) as exc:
        sdk.ops.list_preapprovals("no-such-darwin")
    assert exc.value.status == 404
