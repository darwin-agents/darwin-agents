"""Internal platform endpoints (doc 08 + doc 09 level 4).

Covers:
- Quarantine state machine: pause-+-flag semantics, owner ``resume`` is
  refused while quarantined, ``unquarantine`` clears the flag without
  auto-resuming, killed Darwins reject quarantine.
- Wallet policy: tightens per-action / per-day caps below HARD_CAPS;
  refuses negative caps; the wallet then rejects spends above the new cap.
- Signing service surface: one platform key shared across the fleet.
- The HTTP wiring of all of the above.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

import pytest

from darwin_dna.hyperparameters import HARD_CAPS
from darwin_fakes.fake_wallet import FakeWallet, WalletError
from darwin_platform import Supervisor, SupervisorError


async def _wait_until(
    pred: Callable[[], bool], *, timeout: float = 5.0, tick: float = 0.01
) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(tick)


@pytest.fixture
async def supervisor():
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        yield sup
    finally:
        await sup.shutdown()


# -- quarantine -----------------------------------------------------------
async def test_quarantine_pauses_and_flags(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=60.0)
    h = supervisor._darwins[status.darwin_id]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 3)

    await supervisor.quarantine(status.darwin_id, reason="compliance review")
    await _wait_until(lambda: h.runtime.state.paused)
    after = await supervisor.status(status.darwin_id)
    assert after.quarantined is True
    assert after.quarantine_reason == "compliance review"
    assert after.paused is True


async def test_owner_resume_refused_while_quarantined(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=60.0)
    await supervisor.quarantine(status.darwin_id, reason="policy issue")
    with pytest.raises(SupervisorError, match="quarantined"):
        await supervisor.resume(status.darwin_id)


async def test_unquarantine_clears_flag_but_does_not_resume(
    supervisor: Supervisor,
) -> None:
    status = await supervisor.spawn(opening_usdc=60.0)
    h = supervisor._darwins[status.darwin_id]  # noqa: SLF001
    await supervisor.quarantine(status.darwin_id, reason="x")
    await _wait_until(lambda: h.runtime.state.paused)

    await supervisor.unquarantine(status.darwin_id)
    after = await supervisor.status(status.darwin_id)
    assert after.quarantined is False
    # Still paused — clearing quarantine and resuming are independent
    # actions so each is independently auditable.
    assert after.paused is True

    # Now the owner CAN resume.
    await supervisor.resume(status.darwin_id)
    await _wait_until(lambda: not h.runtime.state.paused)


async def test_quarantine_killed_darwin_refused(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=60.0)
    await supervisor.kill(status.darwin_id)
    with pytest.raises(SupervisorError, match="killed"):
        await supervisor.quarantine(status.darwin_id, reason="x")


async def test_unquarantine_idempotent(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=60.0)
    # Never quarantined — unquarantine is a no-op, not an error.
    await supervisor.unquarantine(status.darwin_id)


# -- wallet policy --------------------------------------------------------
async def test_set_wallet_policy_tightens_caps(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=60.0)
    new = await supervisor.set_wallet_policy(
        status.darwin_id,
        per_action_cap_usdc=10.0,
        per_day_cap_usdc=20.0,
    )
    assert new["per_action_cap_usdc"] == pytest.approx(10.0)
    assert new["per_day_cap_usdc"] == pytest.approx(20.0)
    # Hard caps are also reported so operators can see the headroom.
    assert new["hard_per_action_usdc"] == HARD_CAPS.per_action_usdc
    assert new["hard_per_day_usdc"] == HARD_CAPS.per_day_usdc


async def test_set_wallet_policy_cannot_loosen_above_hard_cap(
    supervisor: Supervisor,
) -> None:
    status = await supervisor.spawn(opening_usdc=60.0)
    new = await supervisor.set_wallet_policy(
        status.darwin_id,
        per_action_cap_usdc=HARD_CAPS.per_action_usdc * 10.0,
    )
    assert new["per_action_cap_usdc"] == HARD_CAPS.per_action_usdc


async def test_wallet_refuses_spend_above_policy_cap() -> None:
    """Direct FakeWallet test — the wallet itself enforces the policy cap,
    not just the supervisor's view of it."""
    w = FakeWallet(opening_usdc=200.0, clock=lambda: 1_000_000.0)
    w.set_policy(per_action_cap_usdc=10.0)
    with pytest.raises(WalletError, match="hard cap"):
        w.pay(to="x", amount_usdc=15.0, memo="too big")


async def test_wallet_refuses_negative_policy() -> None:
    w = FakeWallet(opening_usdc=200.0, clock=lambda: 1_000_000.0)
    with pytest.raises(WalletError, match="non-negative"):
        w.set_policy(per_action_cap_usdc=-1.0)


# -- signing service -------------------------------------------------------
async def test_signing_public_key_available_before_any_spawn() -> None:
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        pem = sup.signing_public_key_pem()
        assert pem.startswith(b"-----BEGIN PUBLIC KEY")
    finally:
        await sup.shutdown()


async def test_signing_key_shared_across_fleet(supervisor: Supervisor) -> None:
    """The platform has ONE key; every Darwin's manifest must verify
    against the same public key (real deployments are HSM-backed and
    multi-tenant; here we model one platform-wide signer)."""
    a = await supervisor.spawn(opening_usdc=60.0)
    b = await supervisor.spawn(opening_usdc=40.0)
    ha = supervisor._darwins[a.darwin_id]  # noqa: SLF001
    hb = supervisor._darwins[b.darwin_id]  # noqa: SLF001
    pem_platform = supervisor.signing_public_key_pem()
    assert ha.stack.signer.public_key_pem() == pem_platform
    assert hb.stack.signer.public_key_pem() == pem_platform


# -- HTTP wiring ----------------------------------------------------------
async def test_quarantine_and_policy_endpoints() -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from darwin_platform.api import build_app  # type: ignore

    sup = Supervisor(heartbeat_seconds=0.0)
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            r = await c.post("/darwins", json={"opening_usdc": 60.0})
            did = r.json()["darwin_id"]
            h = sup._darwins[did]  # noqa: SLF001

            # Quarantine.
            r = await c.post(
                f"/platform/darwins/{did}/quarantine",
                json={"reason": "compliance", "operator": "alice"},
            )
            assert r.status_code == 200
            await _wait_until(lambda: h.runtime.state.paused)

            r = await c.get(f"/darwins/{did}")
            assert r.json()["quarantined"] is True
            assert r.json()["quarantine_reason"] == "compliance"

            # Owner resume → 409 (refused).
            r = await c.post(f"/darwins/{did}/resume")
            assert r.status_code == 409

            # Unquarantine + then resume → 200.
            r = await c.delete(f"/platform/darwins/{did}/quarantine")
            assert r.status_code == 200
            r = await c.post(f"/darwins/{did}/resume")
            assert r.status_code == 200

            # Wallet policy.
            r = await c.post(
                f"/platform/wallets/{did}/policy",
                json={"per_action_cap_usdc": 5.0, "per_day_cap_usdc": 10.0},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["per_action_cap_usdc"] == pytest.approx(5.0)

            r = await c.get(f"/platform/wallets/{did}/policy")
            assert r.json()["per_action_cap_usdc"] == pytest.approx(5.0)

            # Negative policy → 422 (pydantic ge=0).
            r = await c.post(
                f"/platform/wallets/{did}/policy",
                json={"per_action_cap_usdc": -1.0},
            )
            assert r.status_code == 422

            # Unknown id → 404.
            r = await c.post(
                "/platform/darwins/nope/quarantine",
                json={"reason": "x"},
            )
            assert r.status_code == 404

            # Signing public key.
            r = await c.get("/platform/signing/public_key")
            body = r.json()
            assert body["format"] == "PEM"
            assert "BEGIN PUBLIC KEY" in body["public_key"]
        finally:
            await sup.shutdown()
