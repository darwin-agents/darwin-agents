"""Pod Darwin reproduction (``--pod-reproduction``), end to end in one process.

The parent's genome runs in a real pod (watch-only wallet, verify-only
signer). It asks for a child; the supervisor re-verifies the parent's DNA,
signs the child with the platform key, funds it on-chain from the parent's
account, and spawns it for the same owner. Covers: the child boots from the
supervisor-signed DNA with the budget at its own address, the parent books
the outflow with zero drift and a fresh profit window, the child belongs to
the parent's owner, and a second request the same day is refused.
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

FEE = "0x" + "f" * 40
PAYER = "0x" + "c" * 40


class _Chain:
    """Every Darwin's USDC account on one stub chain."""

    def __init__(self) -> None:
        self.block, self.n = 100, 0
        self.logs: list[dict] = []
        self.history: dict[str, dict[int, float]] = {}

    def balance(self, addr: str, block: int | None = None) -> float:
        hist = self.history.get(addr, {0: 0.0})
        top = self.block if block is None else block
        return hist[max(b for b in hist if b <= top)]

    def land(self, frm: str, to: str, amount: float) -> str:
        self.n += 1
        self.block += 1
        tx = f"0x{self.n:064x}"
        for addr, delta in ((frm, -amount), (to, amount)):
            h = self.history.setdefault(addr, {0: 0.0})
            h[self.block] = self.balance(addr) + delta
        self.logs.append({"tx_hash": tx, "block": self.block, "log_index": 0,
                          "from": frm, "to": to, "amount_usdc": amount})
        return tx


class _Account:
    """One Darwin's account: the supervisor's signer and the pod's reader."""

    def __init__(self, chain: _Chain, addr: str) -> None:
        self.chain, self.addr = chain, addr

    def address(self) -> str:
        return self.addr

    def balance_usdc(self) -> float:
        return self.chain.balance(self.addr)

    def transfer(self, *, to: str, amount_usdc: float, memo: str) -> dict:
        return {"tx_hash": self.chain.land(self.addr, to, amount_usdc)}

    def head(self) -> int:
        return self.chain.block

    def balance_at(self, block: int) -> float:
        return self.chain.balance(self.addr, block)

    def transfers(self, lo: int, hi: int) -> list[dict]:
        return [lg for lg in self.chain.logs if lo <= lg["block"] <= hi
                and self.addr in (lg["from"], lg["to"])]


class _PodDriver:
    def __init__(self, chain: _Chain, accounts) -> None:
        self.chain, self.accounts = chain, accounts
        self.now = 0.0  # the pods' clock
        self.pods: dict[str, pod.OrchestratorState] = {}
        self.clients: dict[str, httpx.AsyncClient] = {}

    async def start(self, spec):
        from darwin_dna.evolution import boot

        stack = pod._build_stack(  # noqa: SLF001
            darwin_id=spec.darwin_id, opening_usdc=spec.opening_usdc,
            escalation_auto=spec.escalation_auto, manifest_json=spec.manifest_json,
            layer_blobs=spec.layer_blobs, layer_names=spec.layer_names,
            clock=lambda: 0.0,
            platform_public_key_pem=spec.secret_extras[PLATFORM_PUBLIC_KEY_SECRET_KEY].encode(),
        )
        account = self.accounts(spec.darwin_id, None)
        stack.wallet = RealWallet(WatchOnlyChain(account), clock=lambda: 0.0,
                                  reader=account, fee_address=FEE, confirmations=0)
        stack.llm = FakeLLM(wallet=stack.wallet)
        runtime = boot(stack.platform(), sleep=lambda _: None)  # verifies the DNA
        state = pod.OrchestratorState(
            darwin_id=spec.darwin_id, stack=stack, runtime=runtime,
            clock=lambda: self.now, heartbeat_seconds=0.01, pod_reproduction=True,
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
            darwin_id=did, alive=d["alive"], paused=d["paused"], killed=d["killed"],
            tick=d["tick"], days_elapsed=d["days_elapsed"],
            balance_usdc=d["balance_usdc"], crashed=d.get("crashed"),
        )

    async def force_reproduce(self, did, **_):
        await self.control_post(did, "/force-reproduce", {})

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


@pytest.mark.asyncio
async def test_pod_darwin_reproduces_through_the_supervisor(tmp_path):
    chain = _Chain()
    accounts: dict[str, _Account] = {}

    def account(did, clock):
        if did not in accounts:
            accounts[did] = _Account(chain, "0x" + f"{len(accounts) + 1:040x}")
        return accounts[did]

    driver = _PodDriver(chain, account)
    sup = Supervisor(
        k8s_driver=driver, heartbeat_seconds=0.0,
        persistence_path=str(tmp_path / "p.db"),
        pod_substrate={"platform_fee_address": FEE, "pod_reproduction": True},
        pod_signer_factory=account, pod_settle_min_usdc=1e9,
    )
    try:
        status = await sup.spawn(opening_usdc=0.0, actor="owner_1", escalation_auto=None)
        parent_id = status.darwin_id
        parent = driver.pods[parent_id]
        me = accounts[parent_id].address()
        chain.land(PAYER, me, 200.0)  # the owner funds it
        await _until(lambda: parent.runtime.state.balance_usdc > 199)

        await sup.force_reproduce(parent_id, actor="owner_1")
        await _until(lambda: parent._repro is not None)  # noqa: SLF001
        await sup._sync_k8s_pods()  # noqa: SLF001

        # A child exists, booted from supervisor-signed DNA, owned by owner_1.
        child_id = next(d for d in driver.pods if d != parent_id)
        child = driver.pods[child_id]
        assert child.runtime.identity.parent_id == parent_id
        assert child.runtime.identity.generation == 1
        assert sup.owner_of(child_id) == "owner_1"
        budget = chain.logs[-1]["amount_usdc"]
        assert chain.logs[-1]["from"] == me and chain.logs[-1]["to"] == accounts[child_id].addr
        assert budget > 1.0
        await _until(lambda: child.runtime.state.balance_usdc == pytest.approx(budget))

        # The parent books the outflow: no drift, a fresh profit window.
        pst = parent.runtime.state
        await _until(lambda: any("reproduction_transfer" in mv
                                 for mv in parent.stack.wallet.movements()))
        t = pst.tick
        await _until(lambda: pst.tick > t + 3)
        assert pst.alive
        assert not [r for r in parent.runtime.audit.read_records()
                    if r.get("kind") == "event:ledger.reconciliation_failed"]
        assert pst.profit_baseline_usdc == pytest.approx(pst.balance_usdc, abs=1.0)
        assert any(r.get("kind") == "event:reproduction.done"
                   for r in parent.runtime.audit.read_records())

        # The pod waits an hour before asking again; past that, a second
        # child the same day is refused by the supervisor.
        await sup.force_reproduce(parent_id, actor="owner_1")
        await asyncio.sleep(0.1)
        assert parent._repro is None  # noqa: SLF001 - backing off
        driver.now = 3601.0
        await sup.force_reproduce(parent_id, actor="owner_1")
        await _until(lambda: parent._repro is not None)  # noqa: SLF001
        await sup._sync_k8s_pods()  # noqa: SLF001
        assert len(driver.pods) == 2
        assert parent._repro is None and parent._repro_retry_at > 0  # noqa: SLF001
        assert sup.audit_log.entries(darwin_id=parent_id, action="reproduction_refused")
    finally:
        await sup.shutdown()
        await driver.close()
