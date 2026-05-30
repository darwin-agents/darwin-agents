"""HvacVaultClient authentication methods (docs/engineering/16).

token (dev) / approle + kubernetes (prod). Exercised against a stubbed
``hvac.Client`` so no real Vault server is needed; verifies login happens
at construction and that an auth failure transparently re-logs-in and
retries (the no-background-thread renewal path)."""

from __future__ import annotations

import pytest

from darwin_platform.adapters.hvac_vault import HvacVaultClient


class _Forbidden(Exception):
    """Stand-in for hvac.exceptions.Forbidden (matched by class name)."""


class _InvalidPath(Exception):
    """Stand-in for a missing-key error — must NOT trigger re-auth."""


def _fake_hvac(monkeypatch, *, state):
    import hvac

    class _Approle:
        def login(self, *, role_id, secret_id):
            state["logins"] += 1
            state["last_approle"] = (role_id, secret_id)

    class _K8s:
        def login(self, *, role, jwt):
            state["logins"] += 1
            state["last_k8s"] = (role, jwt)

    class _KVv2:
        def create_or_update_secret(self, *, mount_point, path, secret):
            if state.get("fail_put_once"):
                state["fail_put_once"] = False
                raise _Forbidden("permission denied")
            state["put"] = (path, secret)

        def read_secret_version(self, *, mount_point, path, raise_on_deleted_version):
            if path == "missing":
                raise _InvalidPath("no such path")
            return {"data": {"data": {"value": "sekret"}}}

        def delete_metadata_and_all_versions(self, *, mount_point, path):
            state["deleted"] = path

    class _Client:
        def __init__(self, *, url):
            self.url = url
            self.token = None
            self.auth = type("A", (), {"approle": _Approle(), "kubernetes": _K8s()})()
            self.secrets = type("S", (), {"kv": type("K", (), {"v2": _KVv2()})()})()

    monkeypatch.setattr(hvac, "Client", _Client)


def test_token_auth_requires_token():
    with pytest.raises(ValueError, match="token"):
        HvacVaultClient(url="https://v", auth_method="token")


def test_approle_requires_role_and_secret():
    with pytest.raises(ValueError, match="role_id"):
        HvacVaultClient(url="https://v", auth_method="approle", role_id="r")


def test_approle_logs_in_at_construction(monkeypatch):
    state = {"logins": 0}
    _fake_hvac(monkeypatch, state=state)
    HvacVaultClient(
        url="https://v", auth_method="approle", role_id="rid", secret_id="sid",
    )
    assert state["logins"] == 1
    assert state["last_approle"] == ("rid", "sid")


def test_kubernetes_logs_in_with_jwt(monkeypatch, tmp_path):
    state = {"logins": 0}
    _fake_hvac(monkeypatch, state=state)
    jwt = tmp_path / "token"
    jwt.write_text("the-sa-jwt\n")
    HvacVaultClient(
        url="https://v", auth_method="kubernetes", k8s_role="darwin",
        k8s_jwt_path=str(jwt),
    )
    assert state["logins"] == 1
    assert state["last_k8s"] == ("darwin", "the-sa-jwt")


def test_auth_failure_triggers_reauth_and_retry(monkeypatch):
    state = {"logins": 0, "fail_put_once": True}
    _fake_hvac(monkeypatch, state=state)
    c = HvacVaultClient(
        url="https://v", auth_method="approle", role_id="rid", secret_id="sid",
    )
    assert state["logins"] == 1  # construction login
    c.kv_put("k", "v")           # first put → Forbidden → re-login → retry
    assert state["logins"] == 2
    assert state["put"] == ("k", {"value": "v"})


def test_missing_key_does_not_reauth(monkeypatch):
    state = {"logins": 0}
    _fake_hvac(monkeypatch, state=state)
    c = HvacVaultClient(
        url="https://v", auth_method="approle", role_id="rid", secret_id="sid",
    )
    assert c.kv_get("missing") is None   # InvalidPath → fail-closed None
    assert state["logins"] == 1          # NOT re-authenticated on a 404
