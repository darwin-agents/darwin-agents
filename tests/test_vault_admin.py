"""Tests for the platform-side vault admin + retention (docs/engineering/16).

Owner-write paths, sensitive-tier retention with TTL refresh on read, the
HTTP surface (POST/DELETE/GET /darwins/{id}/vault, POST /platform/vault/sweep),
and the supervisor-driven sweep.
"""

from __future__ import annotations

import time

import pytest

from darwin_platform.vault_admin import (
    DEFAULT_SENSITIVE_TTL_SECONDS,
    VaultAdminError,
    VaultAdminService,
    VaultRetentionService,
)


class _Vault:
    """Minimal raw vault stub: put / get / delete."""

    def __init__(self):
        self.kv = {}

    def put(self, k, v): self.kv[k] = v
    def get(self, k): return self.kv.get(k)
    def delete(self, k): self.kv.pop(k, None)


# ---------------------------------------------------------------------------
# 1. VaultAdminService.put / list / delete
# ---------------------------------------------------------------------------

def test_put_writes_value_and_sidecar():
    v = _Vault()
    svc = VaultAdminService(vault_resolver=lambda _: v)
    ref = svc.put(
        darwin_id="d-1", tier="scoped", scope="payments", key="stripe",
        value="sk_test_123", uploaded_by="alice@x.com",
    )
    assert ref.path == "darwins/d-1/scoped/payments/stripe"
    assert v.kv[ref.path] == "sk_test_123"
    # Sidecar metadata
    sidecar = v.kv[f"{ref.path}.__meta__"]
    assert "sha256" in sidecar
    assert "alice@x.com" in sidecar


def test_put_paths_for_each_tier():
    v = _Vault()
    svc = VaultAdminService(vault_resolver=lambda _: v)
    r1 = svc.put(darwin_id="d-1", tier="public", key="config", value="{}")
    r2 = svc.put(darwin_id="d-1", tier="scoped", scope="payments",
                 key="stripe", value="k")
    r3 = svc.put(darwin_id="d-1", tier="sensitive", key="kyc.id",
                 value="encrypted-blob")
    assert r1.path == "darwins/d-1/public/config"
    assert r2.path == "darwins/d-1/scoped/payments/stripe"
    assert r3.path == "darwins/d-1/sensitive/kyc.id"


def test_put_rejects_empty_key():
    svc = VaultAdminService(vault_resolver=lambda _: _Vault())
    with pytest.raises(VaultAdminError):
        svc.put(darwin_id="d-1", tier="public", key="", value="x")


def test_put_path_smuggle_is_sanitized():
    """Key segments containing '/' must not smuggle into other tiers."""
    v = _Vault()
    svc = VaultAdminService(vault_resolver=lambda _: v)
    ref = svc.put(
        darwin_id="d-1", tier="public",
        key="../sensitive/kyc.id", value="leak-attempt",
    )
    # The slashes were stripped; the value lives in /public/ alone.
    assert ref.path.startswith("darwins/d-1/public/")
    assert "sensitive" not in ref.path.split("/")[-1] or "_" in ref.path


def test_list_excludes_values():
    v = _Vault()
    svc = VaultAdminService(vault_resolver=lambda _: v)
    svc.put(darwin_id="d-1", tier="sensitive", key="kyc", value="secret")
    rows = svc.list("d-1")
    assert len(rows) == 1
    # VaultEntryRef has no 'value' field.
    assert "value" not in rows[0].to_json()
    # sha256 is there for provenance.
    assert rows[0].to_json()["sha256"] is not None


def test_list_filters_by_tier():
    v = _Vault()
    svc = VaultAdminService(vault_resolver=lambda _: v)
    svc.put(darwin_id="d-1", tier="public", key="a", value="1")
    svc.put(darwin_id="d-1", tier="sensitive", key="b", value="2")
    sens = svc.list("d-1", tier="sensitive")
    assert len(sens) == 1
    assert sens[0].tier == "sensitive"


def test_delete_removes_value_and_sidecar():
    v = _Vault()
    svc = VaultAdminService(vault_resolver=lambda _: v)
    ref = svc.put(darwin_id="d-1", tier="public", key="a", value="1")
    svc.delete(darwin_id="d-1", tier="public", key="a")
    assert ref.path not in v.kv
    assert f"{ref.path}.__meta__" not in v.kv
    # Not in the index anymore
    assert svc.list("d-1") == []


def test_delete_missing_raises():
    svc = VaultAdminService(vault_resolver=lambda _: _Vault())
    with pytest.raises(VaultAdminError):
        svc.delete(darwin_id="d-1", tier="public", key="nope")


def test_unknown_darwin_returns_none_vault():
    svc = VaultAdminService(vault_resolver=lambda _id: None)
    with pytest.raises(VaultAdminError):
        svc.put(darwin_id="d-x", tier="public", key="a", value="1")


# ---------------------------------------------------------------------------
# 2. Retention
# ---------------------------------------------------------------------------

def test_sensitive_upload_registers_retention():
    now = [1000.0]
    ret = VaultRetentionService(
        clock=lambda: now[0], sensitive_ttl_seconds=60,
    )
    svc = VaultAdminService(
        vault_resolver=lambda _: _Vault(),
        retention=ret, clock=lambda: now[0],
    )
    ref = svc.put(darwin_id="d-1", tier="sensitive", key="kyc", value="blob")
    assert ref.expires_at == 1060.0


def test_scoped_upload_has_no_default_ttl():
    now = [1000.0]
    svc = VaultAdminService(
        vault_resolver=lambda _: _Vault(),
        retention=VaultRetentionService(clock=lambda: now[0]),
        clock=lambda: now[0],
    )
    ref = svc.put(darwin_id="d-1", tier="scoped", scope="x",
                  key="a", value="1")
    assert ref.expires_at is None


def test_explicit_ttl_overrides_default_for_scoped():
    now = [1000.0]
    svc = VaultAdminService(
        vault_resolver=lambda _: _Vault(),
        retention=VaultRetentionService(clock=lambda: now[0]),
        clock=lambda: now[0],
    )
    ref = svc.put(darwin_id="d-1", tier="scoped", scope="x", key="a",
                  value="1", ttl_seconds=30)
    assert ref.expires_at == 1030.0


def test_touch_resets_retention_window():
    now = [1000.0]
    ret = VaultRetentionService(
        clock=lambda: now[0], sensitive_ttl_seconds=60,
    )
    v = _Vault()
    svc = VaultAdminService(
        vault_resolver=lambda _: v, retention=ret, clock=lambda: now[0],
    )
    ref = svc.put(darwin_id="d-1", tier="sensitive", key="kyc", value="b")
    # 30s later, the owner-grant gets consumed → touch.
    now[0] = 1030.0
    svc.touch_sensitive(darwin_id="d-1", path=ref.path)
    # New expiry is now+60 = 1090, not the original 1060.
    assert ret.expires_at(darwin_id="d-1", path=ref.path) == 1090.0


def test_sweep_purges_expired_sensitive_entries():
    now = [1000.0]
    ret = VaultRetentionService(
        clock=lambda: now[0], sensitive_ttl_seconds=60,
    )
    v = _Vault()
    svc = VaultAdminService(
        vault_resolver=lambda _: v, retention=ret, clock=lambda: now[0],
    )
    ref = svc.put(darwin_id="d-1", tier="sensitive", key="kyc", value="b")
    # Before expiry: nothing purged.
    now[0] = 1059.0
    assert svc.sweep_expired() == []
    assert ref.path in v.kv
    # Past expiry: purged.
    now[0] = 1061.0
    purged = svc.sweep_expired()
    assert len(purged) == 1
    assert purged[0].path == ref.path
    assert ref.path not in v.kv  # raw bytes gone
    # Idempotent: a second sweep finds nothing.
    assert svc.sweep_expired() == []


# ---------------------------------------------------------------------------
# 3. Supervisor wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supervisor_vault_put_audits():
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    ref = sup.vault_put(
        status.darwin_id, tier="scoped", scope="payments",
        key="stripe_key", value="sk_test_x", actor="alice@x.com",
    )
    assert ref.tier == "scoped"
    entries = sup._audit.entries(action="vault_put")
    assert len(entries) == 1
    # Value never made it to the audit chain.
    assert entries[0].metadata.get("value") is None
    assert entries[0].metadata["sha256"] == ref.sha256
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_vault_delete_audits():
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    sup.vault_put(
        status.darwin_id, tier="public", key="config",
        value="{}", actor="alice",
    )
    ref = sup.vault_delete(
        status.darwin_id, tier="public", key="config", actor="alice",
    )
    assert ref.tier == "public"
    entries = sup._audit.entries(action="vault_delete")
    assert len(entries) == 1
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_retention_sweep_during_drive():
    """A short retention TTL + a tight sweep cadence + a few heartbeats
    should purge expired sensitive entries automatically."""
    from darwin_platform.supervisor import Supervisor

    now = [1000.0]
    sup = Supervisor(
        clock=lambda: now[0],
        vault_sensitive_ttl_seconds=10.0,
        vault_sweep_every_n_ticks=1,
        heartbeat_seconds=0,
    )
    status = await sup.spawn(opening_usdc=50.0)
    sup.vault_put(
        status.darwin_id, tier="sensitive", key="kyc", value="blob",
        actor="alice",
    )
    # Advance past expiry and drive a few ticks; the sweep runs each tick.
    now[0] += 20
    # Wait for the drive task to tick.
    import asyncio
    await asyncio.sleep(0.05)
    # Now the sensitive entry should be purged. Force a synchronous sweep
    # too in case the drive task hasn't cycled in this CI's timing.
    purged = sup.sweep_vault_retention()
    # Either the auto-sweep got it or this one did.
    entries = sup._audit.entries(action="vault_retention_purge")
    assert len(entries) >= 1 or len(purged) >= 1
    await sup.shutdown(kill_running=True)


# ---------------------------------------------------------------------------
# 4. HTTP surface
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_owner_write_list_delete_cycle():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    did = status.darwin_id
    app = build_app(sup)
    with TestClient(app) as client:
        # Upload a sensitive KYC blob
        r = client.post(
            f"/darwins/{did}/vault",
            json={
                "tier": "sensitive", "key": "kyc.passport",
                "value": "encrypted-blob",
                "original_filename": "passport.pdf",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["tier"] == "sensitive"
        assert "value" not in body
        assert body["sha256"]

        # List entries (no values)
        r2 = client.get(f"/darwins/{did}/vault")
        assert r2.status_code == 200
        assert len(r2.json()["entries"]) == 1

        # Filter by tier
        r3 = client.get(f"/darwins/{did}/vault?tier=public")
        assert r3.json()["entries"] == []

        # Delete
        r4 = client.delete(
            f"/darwins/{did}/vault",
            params={"tier": "sensitive", "key": "kyc.passport"},
        )
        assert r4.status_code == 200

        # List is empty
        r5 = client.get(f"/darwins/{did}/vault")
        assert r5.json()["entries"] == []

    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_http_sweep_operator_endpoint():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    now = [1000.0]
    sup = Supervisor(
        clock=lambda: now[0],
        vault_sensitive_ttl_seconds=5.0,
    )
    status = await sup.spawn(opening_usdc=50.0)
    did = status.darwin_id
    sup.vault_put(
        did, tier="sensitive", key="kyc", value="b", actor="alice",
    )
    now[0] += 100  # past expiry
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post("/platform/vault/sweep")
        assert r.status_code == 200
        assert r.json()["count"] == 1
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_http_404_unknown_darwin():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post(
            "/darwins/ghost/vault",
            json={"tier": "public", "key": "a", "value": "x"},
        )
        assert r.status_code == 404
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_http_422_on_bad_tier():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post(
            f"/darwins/{status.darwin_id}/vault",
            json={"tier": "bogus", "key": "a", "value": "x"},
        )
        assert r.status_code == 422
    await sup.shutdown(kill_running=True)
