"""E2E: bearer auth and tenant isolation on the real ``serve`` process.

The integration tier proves the scope rules in-process against an
``InMemoryTokenStore``. This tier proves the deployed shape: the
``--auth-tokens`` file is parsed by the CLI, the guard sits in front of
every route on the live socket, the unauthenticated probes stay open for
the kubelet, and a resumed Darwin stays out of other tenants' reach after a
supervisor restart (doc 07 tenant isolation, doc 08 auth, doc 24 cross-tenant read).
"""

from __future__ import annotations

import json


def test_probes_are_public_but_the_api_is_not(server_factory, tokens):
    srv = server_factory(extra_args=["--auth-tokens", str(tokens.path)]).start()
    with srv.client() as anon:
        # Liveness / readiness must answer without a token (kubelet probes).
        assert anon.get("/healthz").status_code == 200
        assert anon.get("/readyz").status_code == 200

        r = anon.post("/darwins", json={"opening_usdc": 50.0})
        assert r.status_code == 401
        assert "Bearer" in r.headers.get("www-authenticate", "")
        assert anon.get("/darwins").status_code == 401

    with srv.client("not-a-real-token") as forged:
        assert forged.get("/darwins").status_code == 401


def test_owners_are_isolated_and_operators_see_the_fleet(server_factory, tokens):
    srv = server_factory(extra_args=["--auth-tokens", str(tokens.path)]).start()
    alice, bob, ops = srv.client(tokens.alice), srv.client(tokens.bob), srv.client(tokens.operator)
    try:
        r = alice.post("/darwins", json={"opening_usdc": 60.0})
        assert r.status_code == 201, r.text
        alice_id = r.json()["darwin_id"]
        bob_id = bob.post("/darwins", json={"opening_usdc": 40.0}).json()["darwin_id"]

        # Each owner lists only their own Darwin.
        assert alice.get("/darwins").json()["darwins"] == [alice_id]
        assert bob.get("/darwins").json()["darwins"] == [bob_id]

        # Bob can neither read nor act on Alice's Darwin — over the wire,
        # every per-Darwin surface refuses, not just the status route.
        for path in ("", "/events", "/ledger", "/lineage", "/controls", "/anatomy"):
            assert bob.get(f"/darwins/{alice_id}{path}").status_code == 403, path
        assert bob.post(f"/darwins/{alice_id}/kill", json={"reason": "x"}).status_code == 403
        assert alice.get(f"/darwins/{alice_id}").json()["alive"] is True

        # Fleet-wide surfaces are operator-only.
        assert alice.get("/platform/snapshot").status_code == 403
        assert ops.get("/platform/snapshot").status_code == 200
        assert set(ops.get("/darwins").json()["darwins"]) == {alice_id, bob_id}
        assert ops.get(f"/darwins/{alice_id}").status_code == 200
    finally:
        for c in (alice, bob, ops):
            c.close()


def test_restart_resumes_darwin_and_honours_owned_darwins(
    server_factory, tokens, tmp_path, wait_for,
):
    """After a SIGKILL + restart on the same SQLite db, the Darwin resumes
    and the static tokens file's ``owned_darwins`` grant is what gives an
    owner access back (the static file is the dev-only auth path — Clerk
    re-seeds ownership from the persisted ``owner_subject`` instead).
    Another owner stays locked out either way."""
    db = str(tmp_path / "state.db")

    srv1 = server_factory(db=db, extra_args=["--auth-tokens", str(tokens.path)]).start()
    with srv1.client(tokens.alice) as alice:
        did = alice.post("/darwins", json={"opening_usdc": 60.0}).json()["darwin_id"]
        wait_for(lambda: alice.get(f"/darwins/{did}").json()["tick"] > 3, what="first ticks")
    srv1.stop(sig="kill")

    entries = json.loads(tokens.path.read_text())
    entries[0]["owned_darwins"] = [did]  # alice
    tokens.path.write_text(json.dumps(entries))

    srv2 = server_factory(db=db, extra_args=["--auth-tokens", str(tokens.path)]).start()
    with srv2.client(tokens.operator) as ops:
        assert did in ops.get("/darwins").json()["darwins"], srv2.log_tail()
    with srv2.client(tokens.alice) as alice, srv2.client(tokens.bob) as bob:
        assert alice.get(f"/darwins/{did}").json()["alive"] is True
        assert bob.get(f"/darwins/{did}").status_code == 403
        assert did not in bob.get("/darwins").json()["darwins"]
