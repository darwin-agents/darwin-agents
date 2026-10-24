"""Pod Darwin money + escalation flow, end to end, in one process.

The real pod (``OrchestratorState`` + its control app over ASGI) runs a genome
on a watch-only wallet over a stub chain; the supervisor signs every outbound
transfer through the same chain (pods never sign). Covers: the Darwin ticks
with zero drift, an escalation reaches the owner and the answer reaches the
pod, a harvest is booked without drift, accrued metering settles to the fee
address, and kill settles + sweeps the rest to the owner's payout address.
"""

from __future__ import annotations

import asyncio

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from darwin_fakes.fake_llm import FakeLLM  # noqa: E402
from darwin_platform.adapters.wallet import RealWallet, WatchOnlyChain  # noqa: E402
from darwin_platform.k8s.manifests import PLATFORM_PUBLIC_KEY_SECRET_KEY  # noqa: E402
from darwin_platform.runtime import orchestrator as pod  # noqa: E402
from darwin_platform.runtime.driver import DarwinSnapshot  # noqa: E402
from darwin_platform.runtime.supervisor import Supervisor  # noqa: E402

ME = "0x" + "a" * 40
FEE = "0x" + "f" * 40
OWNER = "0x" + "b" * 40
PAYER = "0x" + "c" * 40


class _Chain:
    """Reader for ME's USDC and the supervisor's signer for ME's account."""

    def __init__(self, opening: float) -> None:
        self.block, self.n = 100, 0
        self.logs: list[dict] = []
        self.balances = {self.block: opening}

    def land(self, frm: str, to: str, amount: float) -> str:
        self.n += 1
        tx = f"0x{self.n:064x}"
        self.block += 1
        delta = (amount if to == ME else 0.0) - (amount if frm == ME else 0.0)
        self.balances[self.block] = self.balances[max(self.balances)] + delta
        self.logs.append({"tx_hash": tx, "block": self.block, "log_index": 0,
                          "from": frm, "to": to, "amount_usdc": amount})
        return tx

    def address(self) -> str:
        return ME

    def balance_usdc(self) -> float:
        return self.balances[max(self.balances)]

    def transfer(self, *, to: str, amount_usdc: float, memo: str) -> dict:
        return {"tx_hash": self.land(ME, to, amount_usdc)}

    def head(self) -> int:
        return self.block

    def balance_at(self, block: int) -> float:
        return self.balances[max(b for b in self.balances if b <= block)]

    def transfers(self, lo: int, hi: int) -> list[dict]:
        return [lg for lg in self.logs if lo <= lg["block"] <= hi]


class _PodDriver:
    """Boots the real pod for each spawn and routes control RPC to its app."""

    def __init__(self, chain: _Chain) -> None:
        self.chain = chain
        self.pods: dict[str, pod.OrchestratorState] = {}
        self.clients: dict[str, httpx.AsyncClient] = {}

    async def start(self, spec):
        from darwin_dna.evolution import boot

        stack = pod._build_stack(  # noqa: SLF001
            darwin_id=spec.darwin_id, opening_usdc=spec.opening_usdc,
            escalation_auto=spec.escalation_auto,
            manifest_json=spec.manifest_json, layer_blobs=spec.layer_blobs,
            layer_names=spec.layer_names, clock=lambda: 0.0,
            platform_public_key_pem=spec.secret_extras[
                PLATFORM_PUBLIC_KEY_SECRET_KEY].encode(),
        )
        stack.wallet = RealWallet(
            WatchOnlyChain(self.chain), clock=lambda: 0.0, reader=self.chain,
            fee_address=FEE, confirmations=0,
        )
        stack.llm = FakeLLM(wallet=stack.wallet)
        runtime = boot(stack.platform(), sleep=lambda _: None)
        state = pod.OrchestratorState(
            darwin_id=spec.darwin_id, stack=stack, runtime=runtime,
            clock=lambda: 0.0, heartbeat_seconds=0.01,
        )
        state.start()
        self.pods[spec.darwin_id] = state
        self.clients[spec.darwin_id] = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=pod._build_control_app(state, "t")),  # noqa: SLF001
            base_url="http://pod", headers={"Authorization": "Bearer t"},
        )
        return await self.snapshot(spec.darwin_id)

    async def _call(self, did, method, path, body=None):
        r = await self.clients[did].request(method, path, json=body)
        r.raise_for_status()
        return r.json()

    async def control_get(self, did, path):
        return await self._call(did, "GET", path)

    async def control_post(self, did, path, body=None):
        return await self._call(did, "POST", path, body or {})

    async def snapshot(self, did):
        d = await self.control_get(did, "/snapshot")
        return DarwinSnapshot(
            darwin_id=did, alive=d["alive"], paused=d["paused"],
            killed=d["killed"], tick=d["tick"], days_elapsed=d["days_elapsed"],
            balance_usdc=d["balance_usdc"], crashed=d.get("crashed"),
        )

    async def kill(self, did, *, reason):
        await self.control_post(did, "/kill", {"reason": reason})

    async def close(self):
        for state in self.pods.values():
            await state.stop()


async def _until(pred, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _drift_events(state) -> list:
    return [r for r in state.runtime.audit.read_records()
            if r.get("kind") == "event:ledger.reconciliation_failed"]


@pytest.mark.asyncio
async def test_pod_darwin_money_and_escalations_end_to_end(tmp_path):
    chain = _Chain(50.0)
    driver = _PodDriver(chain)
    sup = Supervisor(
        k8s_driver=driver, heartbeat_seconds=0.0,
        persistence_path=str(tmp_path / "p.db"),
        pod_substrate={"platform_fee_address": FEE},
        pod_signer_factory=lambda did, clock: chain,
        pod_settle_min_usdc=0.01,
    )
    try:
        status = await sup.spawn(opening_usdc=50.0, actor="owner_1", escalation_auto=None)
        did = status.darwin_id
        p = driver.pods[did]
        st = p.runtime.state
        await _until(lambda: st.tick >= 5)
        assert p.stack.wallet.unsettled_usdc > 0  # metering accrues, no txs

        # An escalation reaches the owner; the owner's answer reaches the pod.
        p.stack.escalation.post({"ticket": "T-1", "neuron": "x",
                                 "ticket_class": "approval", "darwin_id": did})
        await sup._sync_k8s_pods()  # noqa: SLF001
        assert any(t["ticket_id"] == "T-1" for t in await sup.open_escalations(did))
        await sup.respond_escalation(did, escalation_id="T-1", decision="deny",
                                     actor="owner_1")
        assert p.stack.escalation.poll_responses(did)[-1]["decision"] == "deny"

        # Revenue lands, the owner harvests: supervisor signs, pod books it.
        chain.land(PAYER, ME, 20.0)
        await _until(lambda: st.balance_usdc > 60)
        out = await sup.harvest(did, amount_usdc=10.0, destination=OWNER,
                                actor="owner_1")
        assert out["tx_hash"] and out["net_usdc"] + out["fee_usdc"] == pytest.approx(10.0)
        await _until(lambda: any("harvest" in mv and mv["harvest"]["kind"] == "owner_net"
                                 for mv in p.stack.wallet.movements()))

        # Settlement runs on every sync once unsettled crosses the floor, so
        # metering may already have paid on the escalation sync above; the
        # harvest fee then settles here. Either way the fee address is paid
        # and nothing stays accrued.
        await _until(lambda: any(lg["to"] == FEE for lg in chain.logs))
        await sup._sync_k8s_pods()  # noqa: SLF001
        await _until(lambda: p.stack.wallet.unsettled_usdc < 0.02)
        assert [lg["to"] for lg in chain.logs].count(FEE) >= 1
        t = st.tick
        await _until(lambda: st.tick > t + 3)
        assert st.alive and _drift_events(p) == []

        # Kill: settle what's left, sweep the rest to the payout address.
        sup._lifecycle.set_payout_address(did, address=OWNER, set_at=0.0)  # noqa: SLF001
        await sup.kill(did, actor="owner_1")
        swept = [lg for lg in chain.logs if lg["to"] == OWNER]
        assert len(swept) >= 2  # harvest net + the closing sweep
        assert chain.balance_usdc() == pytest.approx(0.0, abs=0.02)
    finally:
        await sup.shutdown()
        await driver.close()
