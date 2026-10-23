"""Per-Darwin orchestrator entrypoint (docs/engineering/08).

This is the executable inside the per-Darwin pod that the
:class:`darwin_platform.k8s.K8sDarwinDriver` provisions. It owns:

- Booting *one* genome runtime, hydrating from the sealed manifest +
  layer blobs the supervisor wrote into ``/etc/darwin/...`` via a
  ConfigMap. Without those, it mints genesis (dev-only fallback).
- The drive loop: one heartbeat per iteration, ``asyncio.sleep`` between.
  Mirrors the cycle the in-process supervisor runs today (observe
  spawner, ticks, billing, escalations, monitor, harvest, vault sweep).
- A small FastAPI **control API** so the supervisor pod can pause,
  resume, kill, force-reproduce, and read a status snapshot. Bearer-
  token auth — the token is mounted from the per-Darwin Secret.
- Audit + lineage writes go to shared Postgres (when configured), so
  the supervisor pod can aggregate fleet-wide views by reading from the
  same backend.

Run::

    python -m darwin_platform.orchestrator \\
        --darwin-id X \\
        --manifest-path /etc/darwin/manifest.json \\
        --layer-blob-dir /etc/darwin \\
        --layer-names-path /etc/darwin/layer_names.json \\
        --opening-usdc 100 \\
        --control-port 8001 \\
        --control-token-path /run/secrets/darwin-control/control_token

The image is the *same* darwin-platform image — only the entrypoint
differs. That keeps the build pipeline simple and ensures the
orchestrator runs exactly the code the supervisor was built against.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable


# -- bootstrap helpers -----------------------------------------------------
def _load_manifest_and_blobs(
    manifest_path: Path,
    layer_blob_dir: Path,
    layer_names_path: Path,
) -> tuple[str | None, dict[str, bytes], list[str]]:
    """Load the sealed DNA the supervisor wrote into the ConfigMap.

    Returns ``(manifest_json, layer_blobs, layer_names)``. All three
    optional — when none exist we fall back to fresh genesis (dev-only)."""
    manifest_json: str | None = None
    if manifest_path.exists():
        manifest_json = manifest_path.read_text()
    layer_names: list[str] = []
    if layer_names_path.exists():
        try:
            layer_names = json.loads(layer_names_path.read_text())
        except Exception:
            layer_names = []
    layer_blobs: dict[str, bytes] = {}
    # ConfigMap binaryData lands as files named "layer-<name>" in the
    # mount directory.
    if layer_blob_dir.exists():
        for child in layer_blob_dir.iterdir():
            if child.is_file() and child.name.startswith("layer-"):
                name = child.name[len("layer-"):]
                layer_blobs[name] = child.read_bytes()
    return manifest_json, layer_blobs, layer_names


def _build_stack(
    *,
    darwin_id: str,
    opening_usdc: float,
    escalation_auto: str | None,
    manifest_json: str | None,
    layer_blobs: dict[str, bytes],
    layer_names: list[str],
    clock: Callable[[], float],
    platform_public_key_pem: bytes | None = None,
):
    """Construct the FakeStack the runtime boots against. Real adapters
    can be swapped in via :func:`_apply_real_adapters` after construction,
    same way the supervisor's spawn flow does it.

    When ``manifest_json`` is non-empty we hydrate the sealed DNA so the
    Darwin keeps the supervisor-minted identity (lossless resume parity).
    """
    from darwin_fakes import FakeStack
    stack = FakeStack(
        opening_usdc=opening_usdc,
        clock=clock,
        escalation_auto=escalation_auto,
    )
    if manifest_json:
        from darwin_dna.manifest import Manifest
        stack.manifest = Manifest.model_validate_json(manifest_json)
        # The ConfigMap carries blobs by name; the platform wants them by
        # layer index (the manifest is the authority on both).
        stack.layer_blobs = {e.index: layer_blobs[e.name] for e in stack.manifest.layers}
        stack.layer_names = {e.index: e.name for e in stack.manifest.layers}
        # Force boot() to reconstruct identity from the manifest, not
        # the throwaway one FakeStack just minted.
        stack.identity = None
        if platform_public_key_pem:
            stack.signer = _VerifyOnlySigner(platform_public_key_pem)
    return stack


class _VerifyOnlySigner:
    """The platform's public key: boot verifies the DNA against it. Signing
    is the supervisor's job, so a pod (running LLM-driven code) cannot."""

    def __init__(self, pem: bytes) -> None:
        self._pem = pem

    def public_key_pem(self) -> bytes:
        return self._pem

    def sign(self, message: bytes) -> bytes:
        raise PermissionError("pods cannot sign; the platform signs DNA")


def _pod_args(substrate_path: Path) -> argparse.Namespace:
    """A full ``serve`` namespace (every flag at its default) overlaid with
    the supervisor-forwarded substrate settings, so the pod's factories see
    the same option surface as the supervisor's — no missing attributes."""
    from .. import __main__ as cli_main

    ns = cli_main.build_parser().parse_args(["serve"])
    if substrate_path.exists():
        vars(ns).update(json.loads(substrate_path.read_text()))
    return ns


def _apply_real_adapters(
    stack, args, clock: Callable[[], float], *, darwin_id: str,
) -> dict[str, Any]:
    """Mirror of darwin_platform.__main__._build_real_factories, applied
    directly to a FakeStack. Each adapter is only swapped in when its
    required flag/env is present; otherwise the fake stays.

    Returns the money-capabilities manifest for this pod (which payment rails
    it actually wired), so the caller can inject it onto the platform before
    boot — the pod's planner then reasons over live rails exactly like the
    in-process supervisor path. The wallet + x402 rails are pod-local truth;
    fiat / sanctions / KYC / omnibus are platform-mediated (enforced in the
    supervisor pod, never run here), so they report ``none`` from the pod's
    own vantage."""
    # ``args`` is a full ``serve`` namespace (see _pod_args), so the pod builds
    # its substrate exactly as the supervisor would in-process. (__main__
    # lives at the darwin_platform package root — one level up.)
    from .. import __main__ as cli_main
    factories = cli_main._build_real_factories(args)  # noqa: SLF001
    # Vault first: a local-vault wallet provider needs the real per-Darwin
    # vault to seal/retrieve its keypair before the wallet is constructed.
    if "vault_factory" in factories:
        stack.vault = factories["vault_factory"]()
    if "wallet_factory" in factories:
        stack.wallet = factories["wallet_factory"](
            darwin_id, clock, stack.vault,
        )
    # Real LLM transport (doc 13). Applied AFTER the wallet so the transport
    # bills the live wallet, and BEFORE browser/x402/sandbox for symmetry
    # with the supervisor's in-process spawn order. The pod authenticates as
    # its own LiteLLM virtual key — injected by the supervisor as
    # DARWIN_LITELLM_MASTER_KEY via the per-Darwin Secret, so the proxy meters
    # this pod's spend separately (the supervisor reconciles it against this
    # Darwin's ledger). darwin_id rides on every call as request metadata.
    # NOTE: without this block the pod would keep the deterministic FakeLLM —
    # the real LiteLLM transport never reached k8s Darwins before.
    if "llm_transport_factory" in factories:
        stack.llm = _invoke_pod_llm_factory(
            factories["llm_transport_factory"], stack.wallet, clock, darwin_id,
        )
    if "browser_factory" in factories:
        stack.browser = factories["browser_factory"](stack.wallet)
    if "x402_factory" in factories:
        stack.x402_client = factories["x402_factory"](stack.wallet)
    if "sandbox_factory" in factories:
        stack.sandbox = factories["sandbox_factory"](stack.wallet)

    # Money-capabilities manifest for this pod. Wallet + x402 are pod-local
    # truth (did we wire a real factory?); network/chains come from the
    # supervisor-forwarded flags. fiat/sanctions/kyc/omnibus are platform-
    # mediated and NOT run in-pod — but the supervisor forwards their resolved
    # shapes via DARWIN_PLATFORM_MONEY_RAILS so the pod's planner sees the FULL
    # money picture (matching the operator endpoint). Absent env → the rails
    # report "none" (in-process spawn, tests, or an older supervisor).
    from ..payments.money_capabilities import (
        assemble_money_capabilities,
        detect_fiat,
        detect_kyc,
        detect_sanctions,
    )

    forwarded: dict[str, Any] = {}
    raw_rails = os.environ.get("DARWIN_PLATFORM_MONEY_RAILS")
    if raw_rails:
        try:
            loaded = json.loads(raw_rails)
            if isinstance(loaded, dict):
                forwarded = loaded
        except (ValueError, TypeError):
            forwarded = {}

    fiat = forwarded.get("fiat") or detect_fiat(None)
    sanctions = forwarded.get("sanctions") or detect_sanctions(None)
    kyc = forwarded.get("kyc") or detect_kyc(None)
    omnibus_enabled = bool((forwarded.get("omnibus") or {}).get("enabled", False))

    chains = [
        c.strip() for c in (args.chain_networks or "base").split(",") if c.strip()
    ]
    return assemble_money_capabilities(
        network="base-mainnet" if cli_main._is_mainnet(args) else "base-sepolia",  # noqa: SLF001
        wallet_provider=args.wallet_provider or "none",
        wallet_live="wallet_factory" in factories,
        chains=chains,
        fiat=fiat,
        sanctions=sanctions,
        kyc=kyc,
        omnibus_enabled=omnibus_enabled,
        x402_live="x402_factory" in factories,
    )


def _load_pod_soma_strategy(platform, soma_root, darwin_id: str) -> None:
    """Inject this Darwin's evolved soma strategy + neuron overlay (doc 33)
    onto the platform so ``boot`` binds them instead of the founding defaults.
    No-op without a soma root; the loader fails closed, so a bad/tampered soma
    simply leaves the platform defaults."""
    if soma_root is None:
        return
    try:
        from ..evolution.soma_loader import SomaLoader

        loader = SomaLoader(Path(soma_root) / darwin_id)
        result = loader.load_strategy()
        if result.loaded and result.strategy is not None:
            platform.strategy = result.strategy
        neurons = loader.load_neurons()
        if neurons:
            platform.neuron_overlay = neurons
    except Exception:
        return


def _install_pod_soma_merge(runtime, soma_root, darwin_id: str, clock):
    """Pin a per-Darwin :class:`SomaMerger` on the genome's ctx so
    apply_mutation writes promoted patches to ``<soma_root>/<darwin_id>`` on
    the pod's filesystem. Mirrors the supervisor's ``_install_soma_merge_hook``
    for k8s mode. Returns the merger (for the death-forensic capture) or
    ``None`` when no soma root is configured — the genome then keeps the
    Phase-0 simulation. Best-effort: a wiring failure must not stop boot."""
    if soma_root is None:
        return None
    try:
        from ..evolution.soma_merge import SomaMerger

        merger = SomaMerger(Path(soma_root) / darwin_id, clock=clock)
        runtime._ctx.soma_merge = merger  # noqa: SLF001 — single owner
        return merger
    except Exception:
        return None


def _invoke_pod_llm_factory(factory, wallet, clock, darwin_id: str):
    """Call the LLM transport factory passing only the kwargs it accepts.

    Mirrors the supervisor's ``_invoke_llm_factory``: the real factory takes
    ``darwin_id`` (for per-request attribution metadata); a bare 2-arg factory
    (older / test) is called positionally. The pod's virtual key arrives as
    the LiteLLM api-key env, so ``virtual_key`` is not threaded here."""
    import inspect
    kwargs: dict = {}
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        params = {}
    if "darwin_id" in params:
        kwargs["darwin_id"] = darwin_id
    return factory(wallet, clock, **kwargs)


# -- control API -----------------------------------------------------------
class OrchestratorState:
    """The single instance the control API holds. Owns the runtime, the
    stack, and the drive task. Methods are async because the drive task
    runs on the same event loop — touching state from the API must yield
    to it cleanly."""

    def __init__(
        self,
        *,
        darwin_id: str,
        stack,
        runtime,
        clock: Callable[[], float],
        heartbeat_seconds: float = 0.005,
        soma_merger=None,
    ) -> None:
        self.darwin_id = darwin_id
        self.stack = stack
        self.runtime = runtime
        self._clock = clock
        self._heartbeat = float(heartbeat_seconds)
        # Per-Darwin SomaMerger when real soma-merge is wired (k8s pod soma
        # root configured). When present, the death forensic carries the real
        # soma file contents; otherwise it's the signed layer fingerprint only.
        self._soma_merger = soma_merger
        self._task: asyncio.Task | None = None
        self._kill_requested = False
        self._reproduce_requested = False
        self._reproduce_amendment: tuple[bytes, str, str | None] | None = None
        self._closed = False
        # Owner runtime controls (doc 09). In ``--driver=k8s`` the genome
        # runs here, so the pod owns this state; the supervisor sets it via
        # the control API and the drive loop mirrors it into runtime.state
        # every heartbeat (same contract as the in-process Supervisor._drive).
        # Reproduction is off in pods: a child needs a supervisor-signed
        # genesis and a funded pod of its own (not built yet), and a pod's
        # signer cannot sign. ponytail: lift once pod-to-supervisor spawn lands.
        self.reproduction_blocked = True
        self.disabled_neurons: set[str] = set()
        self.disabled_categories: set[str] = set()
        # Death-time code fingerprint (doc 05), captured once when the genome
        # dies. The supervisor pulls it via GET /forensics before reaping the
        # pod and persists it to its own forensics store.
        self._death_forensics: dict[str, Any] | None = None
        # "<ExcType>: <msg>" once the drive loop crashed; cleared by resume().
        self.crashed: str | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._drive(), name=f"darwin:{self.darwin_id}")

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass
            self._task = None

    async def _drive(self) -> None:
        try:
            while self.runtime.state.alive and not self._closed:
                if self._reproduce_amendment is not None:
                    blob, name, kill_reason = self._reproduce_amendment
                    try:
                        self.runtime.install_amendment(blob, name)
                    except Exception:
                        pass
                    self._reproduce_amendment = None
                    self._reproduce_requested = True
                    if kill_reason:
                        # Mark for kill after the next reproduction:
                        # implementations may extend this; for now we
                        # set the kill flag immediately after the next
                        # heartbeat returns.
                        self._post_reproduce_kill_reason = kill_reason
                if self._reproduce_requested:
                    self.runtime.state.force_reproduce = True
                    self._reproduce_requested = False
                self.runtime.state.reproduction_blocked = self.reproduction_blocked
                self.runtime.state.disabled_neurons = sorted(self.disabled_neurons)
                self.runtime.state.disabled_categories = sorted(
                    self.disabled_categories
                )
                await self.runtime.run(max_heartbeats=1)
                # Hot-swap the strategy if a soma patch landed this tick (doc
                # 33) — the pod's quiescent boundary, same as the supervisor.
                self._maybe_hot_reload_soma()
                if not self.runtime.state.alive:
                    break
                await asyncio.sleep(self._heartbeat)
            # The organism stopped being alive — capture the pod's death-time
            # code fingerprint for post-mortem forensics (doc 05) so it's ready
            # when the supervisor pulls GET /forensics before reaping the pod.
            self._capture_death_forensics()
        except asyncio.CancelledError:
            if not self._kill_requested:
                self._kill_requested = True
                try:
                    self.stack.kill.flip("orchestrator cancelled")
                except Exception:
                    pass
            raise
        except Exception as exc:
            # The loop is dead: no ticks, no spend. Surface it through the
            # snapshot (the supervisor alerts on it) rather than freezing
            # silently while reporting alive; resume() restarts the loop.
            self.crashed = f"{type(exc).__name__}: {exc}"
            raise

    def _maybe_hot_reload_soma(self) -> None:
        """Hot-swap the strategy mid-life when a soma patch landed this tick
        (doc 33). Consumes the genome's reload signal; reloads via SomaLoader
        rooted at the merger's soma root; rebinds only on a valid load.
        Best-effort + never raises."""
        try:
            if not self.runtime.consume_soma_reload():
                return
        except Exception:
            return
        if self._soma_merger is None:
            return
        try:
            from ..evolution.soma_loader import SomaLoader

            loader = SomaLoader(self._soma_merger.root)
            result = loader.load_strategy()
            if result.loaded and result.strategy is not None:
                self.runtime.rebind_strategy(result.strategy)
            neurons = loader.load_neurons()
            if neurons:
                try:
                    self.runtime.merge_neurons(neurons)
                except Exception:
                    pass  # contract violation → keep prior catalog
        except Exception:
            return

    # -- control verbs --------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        st = self.runtime.state
        try:
            balance = float(self.stack.wallet.balance_usdc())
        except Exception:
            balance = 0.0
        # In-process LLM spend (memo ``llm:*``) so the supervisor can
        # reconcile this pod's spend against the proxy's per-key table
        # (doc 13 §Observability) without shipping the whole ledger.
        try:
            from ..adapters.litellm_admin import ledger_llm_spend_usdc
            llm_spend = ledger_llm_spend_usdc(self.stack.wallet.movements())
        except Exception:
            llm_spend = 0.0
        # The pod's audit Merkle root so the supervisor can fold it into the
        # daily fleet anchor (doc 29) — K8s Darwins keep their hash-chained
        # audit log in the pod, so the root isn't reachable in-process.
        try:
            merkle_root = self.runtime.audit.merkle_root()
        except Exception:
            merkle_root = None
        return {
            "darwin_id": self.darwin_id,
            "alive": bool(st.alive),
            "paused": bool(getattr(st, "paused", False)),
            "killed": bool(self._kill_requested or not st.alive),
            "tick": int(st.tick),
            "days_elapsed": float(st.days_elapsed),
            "balance_usdc": balance,
            "death_reason": st.death_reason,
            "llm_spend_usdc": float(llm_spend),
            "merkle_root": merkle_root,
            "crashed": self.crashed,
            # What the supervisor needs to sign for this Darwin: whose address,
            # what's accrued, and how much is profit (harvest fee base).
            "wallet": {
                "address": getattr(self.stack.wallet, "address", lambda: None)(),
                "unsettled_usdc": getattr(self.stack.wallet, "unsettled_usdc", 0.0),
                "stale": bool(getattr(self.stack.wallet, "stale", False)),
            },
            "profit_baseline_usdc": float(st.profit_baseline_usdc),
            "starting_capital_usdc": float(st.starting_capital_usdc),
        }

    # -- platform-signed money (the pod's wallet is watch-only) -----------
    def escalations_since(self, since: int) -> dict[str, Any]:
        """Tickets the genome posted since cursor ``since`` (a cursor past the
        end means the pod restarted; the supervisor resets to 0)."""
        tickets = self.stack.escalation.tickets
        return {"tickets": tickets[max(0, since):], "next": len(tickets)}

    def respond_escalation(self, ticket: str, decision: str, data: dict) -> None:
        self.stack.escalation.respond(ticket, decision, data)

    def wallet_expect(self, tx_hash: str, movement: dict) -> None:
        expect = getattr(self.stack.wallet, "expect", None)
        if not callable(expect):
            raise ValueError("this wallet does not track on-chain movements")
        expect(tx_hash, movement)

    def wallet_harvest(
        self, *, tx_hash: str, destination: str, net_usdc: float, fee_usdc: float,
    ) -> None:
        """The supervisor sent ``net_usdc`` to the owner in ``tx_hash``;
        book it when it confirms, and accrue the fee for settlement."""
        if net_usdc > 0:
            self.wallet_expect(tx_hash, {"harvest": {
                "destination": destination, "amount_usdc": float(net_usdc),
                "kind": "owner_net", "ts": float(self.clock()),
            }})
        if fee_usdc > 0:
            self.stack.wallet.accrue_harvest_fee(float(fee_usdc))

    def forensics(self) -> dict[str, Any]:
        """The pod's death-time code fingerprint — the k8s analog of the
        in-process soma forensic snapshot (doc 05). A k8s pod has no mutable
        soma tree on disk (soma-merge is in-process only), so 'what code was
        running' is the SIGNED layer manifest: each layer's SHA-256 plus the
        genome identity. That's stable across the pod's life, so this is
        well-defined whether or not the organism has died yet — but it's
        *captured* (frozen) at the moment of death so the reason + final
        state reflect the post-mortem."""
        if self._death_forensics is not None:
            return self._death_forensics
        return self._build_forensics(
            reason=self.runtime.state.death_reason or ""
        )

    def _capture_death_forensics(self) -> None:
        """Freeze the code fingerprint at death. Idempotent + best-effort —
        a capture failure must never keep the drive loop from unwinding."""
        if self._death_forensics is not None:
            return
        try:
            self._death_forensics = self._build_forensics(
                reason=self.runtime.state.death_reason or "death"
            )
        except Exception:
            pass

    def _build_forensics(self, *, reason: str) -> dict[str, Any]:
        st = self.runtime.state
        ts = float(self._clock())
        manifest = getattr(self.runtime, "_p", None)
        manifest = getattr(manifest, "manifest", None)
        layers: list[dict[str, Any]] = []
        identity: dict[str, Any] = {}
        fingerprint = hashlib.sha256()
        if manifest is not None:
            for entry in manifest.layers:
                layers.append({
                    "index": entry.index,
                    "name": entry.name,
                    "sha256": entry.sha256,
                })
                fingerprint.update(entry.name.encode("utf-8"))
                fingerprint.update(entry.sha256.encode("ascii"))
            ident = manifest.identity
            identity = {
                "darwin_id": ident.darwin_id,
                "generation": ident.generation,
                "parent_id": ident.parent_id,
                "l0_version": ident.l0_version,
                "lineage": list(ident.lineage),
            }
        try:
            balance = float(self.stack.wallet.balance_usdc())
        except Exception:
            balance = None
        layer_digest = fingerprint.hexdigest()
        out: dict[str, Any] = {
            "schema": "darwin-platform.soma-forensic-k8s/1",
            "snapshot_id": f"death-{int(ts * 1000)}",
            "darwin_id": self.darwin_id,
            "reason": reason,
            "captured_at": ts,
            # The signed genome layers — always present (the code fingerprint).
            "layers": layers,
            "layer_count": len(layers),
            "layer_digest": layer_digest,
            "identity": identity,
            "metadata": {
                "death_reason": st.death_reason,
                "tick": int(st.tick),
                "alive": bool(st.alive),
                "balance_usdc": balance,
                "manifest_signature": getattr(manifest, "signature", None),
            },
        }
        # When real soma-merge is wired (a pod soma root is configured), overlay
        # the actual soma tree — the files apply_mutation landed — so the k8s
        # forensic carries real contents, not just the layer fingerprint.
        tree = None
        if self._soma_merger is not None:
            try:
                tree = self._soma_merger.soma_tree_contents()
            except Exception:
                tree = None
        if tree is not None:
            out["contents"] = tree["contents"]
            out["files"] = tree["files"]
            out["total_bytes"] = tree["total_bytes"]
            out["soma_digest"] = tree["soma_digest"]
            out["file_count"] = tree["file_count"]
            out["metadata"]["soma_present"] = True
        else:
            # No soma tree: the layer fingerprint stands in (back-compat).
            out["soma_digest"] = layer_digest
            out["file_count"] = len(layers)
            out["total_bytes"] = 0
            out["metadata"]["soma_present"] = False
        return out

    def anatomy(self) -> dict[str, Any]:
        """Structural snapshot of the organism — the pod-side source for
        ``GET /darwins/{id}/anatomy`` in k8s mode. Mirrors the shape of
        ``Supervisor.anatomy`` exactly (the dashboard renders both). The pod
        is single-tenant, and its genome stamps audit records with its own
        genesis identity (independent of the supervisor handle id), so no
        darwin_id filter is applied to the record scan."""
        runtime = self.runtime
        catalog = getattr(runtime, "catalog", {}) or {}
        try:
            records = self.stack.audit_sink.read_all()
        except Exception:
            records = []
        recent = records[-1000:]  # cap the scan; heat is a rolling signal

        last_dispatch: dict[str, float] = {}
        last_error: dict[str, float] = {}
        counts: dict[str, int] = {}
        active_now: set[str] = set()
        for rec in recent:
            kind = str(rec.get("kind", ""))
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            ts = float(rec.get("ts", 0))
            neuron = None
            if kind.startswith("intent:neuron."):
                neuron = kind[len("intent:neuron.") :]
                active_now.add(neuron)
            elif kind == "outcome" and isinstance(payload, dict):
                pn = payload.get("neuron")
                if isinstance(pn, str):
                    neuron = pn
                    active_now.discard(neuron)
                    if payload.get("status") in ("fail", "failed", "refused", "error"):
                        last_error[neuron] = ts
            elif isinstance(payload, dict) and isinstance(payload.get("neuron"), str):
                neuron = payload["neuron"]
            if neuron:
                counts[neuron] = counts.get(neuron, 0) + 1
                last_dispatch[neuron] = max(last_dispatch.get(neuron, 0.0), ts)

        max_count = max(counts.values(), default=1)

        st = runtime.state
        if not st.alive:
            orch_state = "dead"
        elif getattr(st, "paused", False):
            orch_state = "paused"
        else:
            orch_state = "running"

        neurons_payload: list[dict[str, Any]] = []
        edges_payload: list[dict[str, Any]] = []
        heatmap_payload: dict[str, float] = {}
        for name, n in sorted(catalog.items()):
            category = name.split(".", 1)[0] if "." in name else name
            is_disabled = (
                name in self.disabled_neurons
                or category in self.disabled_categories
            )
            ts_last = last_dispatch.get(name)
            ts_err = last_error.get(name)
            count = counts.get(name, 0)
            heat = count / max_count if max_count else 0.0
            heatmap_payload[name] = round(heat, 4)
            if is_disabled:
                status = "disabled"
            elif name in active_now:
                status = "active"
            elif ts_err and (not ts_last or ts_err >= ts_last):
                status = "error"
            else:
                status = "idle"
            neurons_payload.append(
                {
                    "id": name,
                    "label": name,
                    "category": category,
                    "version": getattr(n, "version", None),
                    "status": status,
                    "last_dispatch_at": ts_last,
                    "last_error_at": ts_err,
                    "dispatch_count_recent": count,
                }
            )
            edges_payload.append(
                {
                    "from": "orchestrator",
                    "to": name,
                    "kind": "dispatch",
                    "active": name in active_now,
                    "weight": round(heat, 4),
                }
            )

        manifest = getattr(getattr(runtime, "_p", None), "manifest", None)
        generation = getattr(st, "generation", None)
        layer_names: list[str] = []
        if manifest is not None:
            layer_names = [entry.name for entry in manifest.layers]
            if generation is None:
                generation = manifest.identity.generation

        return {
            "darwin_id": self.darwin_id,
            "orchestrator": {
                "id": "orchestrator",
                "label": "Orchestrator",
                "state": orch_state,
                "tick": int(st.tick),
            },
            "neurons": neurons_payload,
            "edges": edges_payload,
            "heatmap": heatmap_payload,
            "dna": {
                "generation": generation,
                "layers": layer_names,
            },
        }

    def controls(self) -> dict[str, Any]:
        """Owner-control state (doc 09) — same shape as
        ``Supervisor.get_owner_controls``."""
        wallet = self.stack.wallet
        spend_lock = (
            wallet.spend_lock()
            if hasattr(wallet, "spend_lock")
            else {"spend_locked": False, "reason": None}
        )
        return {
            "spend_lock": spend_lock,
            "reproduction_blocked": self.reproduction_blocked,
            "disabled_neurons": sorted(self.disabled_neurons),
            "disabled_categories": sorted(self.disabled_categories),
        }

    def set_spend_lock(self, locked: bool, reason: str | None) -> dict[str, Any]:
        wallet = self.stack.wallet
        if not hasattr(wallet, "set_spend_lock"):
            raise ValueError("wallet does not support spend-lock")
        return wallet.set_spend_lock(
            bool(locked), reason=reason if locked else None,
        )

    def set_reproduction_block(self, blocked: bool) -> dict[str, Any]:
        if not blocked:
            raise ValueError("reproduction is not available for pod-hosted Darwins yet")
        self.reproduction_blocked = True
        return {"reproduction_blocked": self.reproduction_blocked}

    def set_disabled_neurons(self, neurons: list[str]) -> dict[str, Any]:
        self.disabled_neurons = {
            str(n).strip() for n in neurons if str(n).strip()
        }
        return {"disabled_neurons": sorted(self.disabled_neurons)}

    def set_disabled_categories(self, categories: list[str]) -> dict[str, Any]:
        self.disabled_categories = {
            str(c).strip() for c in categories if str(c).strip()
        }
        return {"disabled_categories": sorted(self.disabled_categories)}

    def ledger(self) -> dict[str, Any]:
        """The wallet's full movement feed. The supervisor pages, exports
        and summarizes — this just hands the authoritative rows over."""
        try:
            movements = list(self.stack.wallet.movements())
        except Exception:
            movements = []
        try:
            balance = float(self.stack.wallet.balance_usdc())
        except Exception:
            balance = 0.0
        return {
            "darwin_id": self.darwin_id,
            "balance_usdc": balance,
            "movements": movements,
        }

    # -- wallet policy + pre-approvals (docs 08, 17) -----------------------
    def wallet_policy(self) -> dict[str, Any]:
        wallet = self.stack.wallet
        if not hasattr(wallet, "policy"):
            raise ValueError("wallet does not expose policy")
        return wallet.policy()

    def set_wallet_policy(
        self,
        *,
        per_action_cap_usdc: float | None,
        per_day_cap_usdc: float | None,
        per_lifetime_cap_usdc: float | None,
    ) -> dict[str, Any]:
        wallet = self.stack.wallet
        if not hasattr(wallet, "set_policy"):
            raise ValueError("wallet does not support platform-installed policy")
        return wallet.set_policy(
            per_action_cap_usdc=per_action_cap_usdc,
            per_day_cap_usdc=per_day_cap_usdc,
            per_lifetime_cap_usdc=per_lifetime_cap_usdc,
        )

    def list_preapprovals(self) -> list[dict[str, Any]]:
        wallet = self.stack.wallet
        if not hasattr(wallet, "owner_preapprovals"):
            return []
        return [r.model_dump() for r in wallet.owner_preapprovals()]

    def set_preapproval(self, body: dict[str, Any]) -> dict[str, Any]:
        from darwin_dna.constitution import PreApprovalRule

        wallet = self.stack.wallet
        if not hasattr(wallet, "set_preapproval"):
            raise ValueError("wallet does not support pre-approvals")
        rule = PreApprovalRule(
            rule_id=str(body["rule_id"]),
            max_amount_usdc=float(body["max_amount_usdc"]),
            counterparty_category=body.get("counterparty_category"),
            counterparty_substring=body.get("counterparty_substring"),
            expires_at=body.get("expires_at"),
            max_uses=body.get("max_uses"),
        )
        wallet.set_preapproval(rule)
        return rule.model_dump()

    def revoke_preapproval(self, rule_id: str) -> dict[str, Any]:
        wallet = self.stack.wallet
        if not hasattr(wallet, "revoke_preapproval"):
            raise ValueError("wallet does not support pre-approvals")
        return {
            "revoked": bool(wallet.revoke_preapproval(rule_id)),
            "rule_id": rule_id,
        }

    # -- LLM tier throttle (docs 08, 09 level 2) ---------------------------
    def throttle_llm_tier(self, tier: str, max_daily_calls: int) -> dict[str, Any]:
        from darwin_dna.machinery.llm_router import Tier

        t = Tier(tier)  # ValueError → 400 at the route
        self.runtime.llm.set_throttle(t, int(max_daily_calls))
        return {
            "tier": tier,
            "max_daily_calls": int(max_daily_calls),
            "throttles": self.runtime.llm.throttles(),
        }

    def clear_llm_throttle(self, tier: str | None) -> dict[str, Any]:
        from darwin_dna.machinery.llm_router import Tier

        t = Tier(tier) if tier else None
        self.runtime.llm.clear_throttle(t)
        return {"throttles": self.runtime.llm.throttles()}

    def llm_throttles(self) -> dict[str, int]:
        return self.runtime.llm.throttles()

    # -- Path-C neuron proposals (doc 04 phase 3) --------------------------
    def list_proposals(self) -> dict[str, Any]:
        gate = self.runtime.neuron_admission
        if gate is None:
            return {"pending": [], "admitted": []}
        return {
            "pending": [p.to_json() for p in gate.pending()],
            "admitted": [
                {"name": n.name,
                 "tags": sorted(n.capability_tags),
                 "version": n.version}
                for n in gate.admitted().values()
            ],
        }

    def admit_proposal(self, name: str) -> dict[str, Any]:
        self.runtime.admit_neuron_proposal(name)  # raises on unknown/invalid
        return {"admitted": name}

    def reject_proposal(self, name: str, reason: str) -> dict[str, Any]:
        gate = self.runtime.neuron_admission
        if gate is None:
            raise ValueError(
                "Path-C is disabled; enable path_c_enabled to manage proposals"
            )
        gate.reject(name, reason=reason)
        return {"rejected": name, "reason": reason}

    # -- wallet legs for supervisor-driven money flows (doc 17) ------------
    def wallet_address(self) -> dict[str, Any]:
        wallet = self.stack.wallet
        address_getter = getattr(wallet, "address", None)
        if not callable(address_getter):
            raise ValueError("wallet has no on-chain address")
        return {"address": str(address_getter() or "")}

    def wallet_transfer_out_for_fiat_payout(
        self, amount_usdc: float, destination: str,
    ) -> dict[str, Any]:
        wallet = self.stack.wallet
        transfer = getattr(wallet, "transfer_out_for_fiat_payout", None)
        if not callable(transfer):
            raise ValueError("wallet does not support fiat payout transfer")
        transfer(float(amount_usdc), destination=str(destination))
        return {"transferred": float(amount_usdc), "destination": str(destination)}

    def wallet_credit(self, amount_usdc: float, memo: str) -> dict[str, Any]:
        wallet = self.stack.wallet
        crediter = getattr(wallet, "credit", None)
        if not callable(crediter):
            raise ValueError("wallet does not support credit")
        crediter(float(amount_usdc), str(memo))
        return {"credited": float(amount_usdc), "memo": str(memo)}

    # The timeline only ever shows recent activity, and an immortal pod's
    # durable chain is millions of records — never serialise the whole thing
    # over the control API. Hand back a bounded tail; the supervisor pages
    # within it by seq/cursor.
    EVENTS_TAIL = 5_000

    def events_records(self) -> list[dict[str, Any]]:
        """Recent audit-log records held in this pod (bounded tail).

        In ``--driver=k8s`` the Darwin runs here, so its hash-chained audit
        log lives in this pod's ``audit_sink`` — not in the supervisor. The
        supervisor fetches these via the control API and applies all
        filtering/paging itself; we return the most recent ``EVENTS_TAIL``
        records (streamed from the sink's tail cursor when it has one) so the
        response stays bounded for a long-lived pod.
        """
        sink = self.stack.audit_sink
        try:
            tail = getattr(sink, "read_tail", None)
            if callable(tail):
                return tail(self.EVENTS_TAIL)
            return sink.read_all()[-self.EVENTS_TAIL:]
        except Exception:
            return []

    def pause(self, reason: str) -> None:
        self.stack.pause.pause(reason)

    def resume(self) -> None:
        self.stack.pause.resume()
        if self.crashed and self._task is not None and self._task.done():
            self.crashed = None
            self._task = asyncio.create_task(
                self._drive(), name=f"darwin:{self.darwin_id}"
            )

    def kill(self, reason: str) -> None:
        self._kill_requested = True
        self.stack.kill.flip(reason)

    def force_reproduce(
        self,
        *,
        amendment_blob: bytes | None = None,
        amendment_name: str | None = None,
        kill_parent_reason: str | None = None,
    ) -> None:
        if amendment_blob is not None and amendment_name is not None:
            self._reproduce_amendment = (
                amendment_blob, amendment_name, kill_parent_reason,
            )
        else:
            self._reproduce_requested = True


def _build_control_app(state: OrchestratorState, token: str):
    """Build the FastAPI app. Imported lazily so an orchestrator running
    in a minimal sandbox without [platform] still imports — though it
    won't actually start without uvicorn."""
    try:
        from fastapi import Body, Depends, FastAPI, Header, HTTPException
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "darwin_platform.orchestrator needs the [platform] extra"
        ) from exc

    app = FastAPI(title=f"darwin-orchestrator:{state.darwin_id}")

    def _require_token(authorization: str | None = Header(default=None)) -> None:
        # Defense in depth — even with NetworkPolicy locking ingress to
        # the supervisor pod, the bearer-token check makes a misrouted
        # request fail closed.
        expected = f"Bearer {token}"
        if not authorization or authorization != expected:
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/healthz")
    async def healthz() -> dict:
        # Liveness is "process up". The control loop may be paused but
        # the orchestrator is still serving — that's healthy.
        return {"ok": True}

    @app.get("/snapshot")
    async def snapshot(_: None = Depends(_require_token)) -> dict:
        return state.snapshot()

    @app.get("/events")
    async def events(_: None = Depends(_require_token)) -> dict:
        # Raw records; the supervisor filters + pages (see Supervisor.events).
        return {"records": state.events_records()}

    @app.get("/forensics")
    async def forensics(_: None = Depends(_require_token)) -> dict:
        # The pod's death-time code fingerprint (doc 05). The supervisor pulls
        # this before reaping the pod and persists it to its forensics store.
        return state.forensics()

    @app.get("/anatomy")
    async def anatomy(_: None = Depends(_require_token)) -> dict:
        return state.anatomy()

    @app.get("/controls")
    async def controls(_: None = Depends(_require_token)) -> dict:
        return state.controls()

    @app.get("/ledger")
    async def ledger(_: None = Depends(_require_token)) -> dict:
        return state.ledger()

    @app.post("/controls/spend_lock")
    async def set_spend_lock(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        try:
            return state.set_spend_lock(
                bool(body.get("locked", False)),
                body.get("reason"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/controls/reproduction_block")
    async def set_reproduction_block(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        return state.set_reproduction_block(bool(body.get("blocked", False)))

    @app.post("/controls/disabled_neurons")
    async def set_disabled_neurons(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        neurons = body.get("neurons")
        if not isinstance(neurons, list):
            raise HTTPException(status_code=400, detail="neurons must be a list")
        return state.set_disabled_neurons(neurons)

    @app.post("/controls/disabled_categories")
    async def set_disabled_categories(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        categories = body.get("categories")
        if not isinstance(categories, list):
            raise HTTPException(status_code=400, detail="categories must be a list")
        return state.set_disabled_categories(categories)

    # The control client speaks GET/POST only, so mutations that would be
    # PUT/DELETE on the public API surface here as POST verbs.
    @app.get("/wallet/policy")
    async def wallet_policy(_: None = Depends(_require_token)) -> dict:
        try:
            return state.wallet_policy()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/wallet/policy")
    async def set_wallet_policy(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        try:
            return state.set_wallet_policy(
                per_action_cap_usdc=body.get("per_action_cap_usdc"),
                per_day_cap_usdc=body.get("per_day_cap_usdc"),
                per_lifetime_cap_usdc=body.get("per_lifetime_cap_usdc"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/wallet/preapprovals")
    async def list_preapprovals(_: None = Depends(_require_token)) -> dict:
        return {"rules": state.list_preapprovals()}

    @app.post("/wallet/preapprovals")
    async def set_preapproval(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        try:
            return state.set_preapproval(body)
        except (KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"bad rule: {exc}")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/wallet/preapprovals/revoke")
    async def revoke_preapproval(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        rule_id = body.get("rule_id")
        if not rule_id:
            raise HTTPException(status_code=400, detail="rule_id is required")
        try:
            return state.revoke_preapproval(str(rule_id))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/throttle")
    async def llm_throttles(_: None = Depends(_require_token)) -> dict:
        return {"throttles": state.llm_throttles()}

    @app.post("/throttle")
    async def throttle_llm_tier(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        try:
            return state.throttle_llm_tier(
                str(body.get("tier", "")), int(body.get("max_daily_calls", 0)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/throttle/clear")
    async def clear_llm_throttle(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        tier = body.get("tier")
        try:
            return state.clear_llm_throttle(str(tier) if tier else None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/proposals")
    async def list_proposals(_: None = Depends(_require_token)) -> dict:
        return state.list_proposals()

    @app.post("/proposals/admit")
    async def admit_proposal(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        name = body.get("name")
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        try:
            return state.admit_proposal(str(name))
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/proposals/reject")
    async def reject_proposal(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        name = body.get("name")
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        try:
            return state.reject_proposal(
                str(name), str(body.get("reason", "operator rejected")),
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/wallet/address")
    async def wallet_address(_: None = Depends(_require_token)) -> dict:
        try:
            return state.wallet_address()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/wallet/transfer_out_for_fiat_payout")
    async def wallet_transfer_out_for_fiat_payout(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        amount = body.get("amount_usdc")
        destination = body.get("destination")
        if not amount or not destination:
            raise HTTPException(
                status_code=400, detail="amount_usdc and destination are required",
            )
        try:
            return state.wallet_transfer_out_for_fiat_payout(
                float(amount), str(destination),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            # WalletError etc. — refuse loudly; the supervisor surfaces it.
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/wallet/credit")
    async def wallet_credit(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        amount = body.get("amount_usdc")
        if not amount:
            raise HTTPException(status_code=400, detail="amount_usdc is required")
        try:
            return state.wallet_credit(
                float(amount), str(body.get("memo", "credit")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/escalations")
    async def escalations(since: int = 0, _: None = Depends(_require_token)) -> dict:
        return state.escalations_since(since)

    @app.post("/escalations/respond")
    async def escalations_respond(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        if body.get("decision") not in ("approve", "deny", "data") or not body.get("ticket"):
            raise HTTPException(status_code=400, detail="ticket and decision are required")
        state.respond_escalation(
            str(body["ticket"]), str(body["decision"]), dict(body.get("data") or {}),
        )
        return {"responded": True}

    @app.post("/wallet/expect")
    async def wallet_expect(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        try:
            state.wallet_expect(str(body["tx_hash"]), dict(body["movement"]))
        except (KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"bad movement: {exc}")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"expected": True}

    @app.post("/wallet/harvest")
    async def wallet_harvest(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        try:
            state.wallet_harvest(
                tx_hash=str(body.get("tx_hash") or ""),
                destination=str(body["destination"]),
                net_usdc=float(body["net_usdc"]), fee_usdc=float(body["fee_usdc"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"booked": True}

    @app.post("/pause")
    async def pause(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        state.pause(str(body.get("reason", "supervisor pause")))
        return {"paused": True}

    @app.post("/resume")
    async def resume(_: None = Depends(_require_token)) -> dict:
        state.resume()
        return {"paused": False}

    @app.post("/kill")
    async def kill(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        state.kill(str(body.get("reason", "supervisor kill")))
        return {"killed": True}

    @app.post("/force-reproduce")
    async def force_reproduce(_: None = Depends(_require_token)) -> dict:
        raise HTTPException(
            status_code=409,
            detail="reproduction is not available for pod-hosted Darwins yet",
        )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await state.stop()

    return app


# -- entrypoint ------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="darwin-orchestrator",
        description=(
            "Per-Darwin orchestrator (docs/engineering/08). Owns one "
            "genome's drive loop + a small HTTP control API the "
            "supervisor pod calls into."
        ),
    )
    p.add_argument("--darwin-id", required=True)
    p.add_argument(
        "--manifest-path", default="/etc/darwin/manifest.json", type=Path,
        help="Sealed DNA manifest (from the per-Darwin ConfigMap).",
    )
    p.add_argument(
        "--layer-blob-dir", default="/etc/darwin", type=Path,
        help="Directory holding ConfigMap binaryData entries (layer-*).",
    )
    p.add_argument(
        "--layer-names-path", default="/etc/darwin/layer_names.json", type=Path,
        help="Ordered list of layer names.",
    )
    p.add_argument("--opening-usdc", default=100.0, type=float)
    p.add_argument(
        "--escalation-auto", default="none",
        choices=["approve", "deny", "none"],
        help="Unattended owner-response policy (use 'none' to disable).",
    )
    p.add_argument(
        "--control-port", default=8001, type=int,
        help="TCP port for the orchestrator's control API.",
    )
    p.add_argument(
        "--control-token-path",
        default="/run/secrets/darwin-control/control_token",
        type=Path,
        help="Path to the bearer token file mounted from the Secret.",
    )
    p.add_argument(
        "--soma-root", default=None, type=Path,
        help=(
            "Root for this pod's mutable soma tree (docs 03/21/05). When set, "
            "apply_mutation writes promoted patches to <soma-root>/<darwin-id> "
            "on disk (real self-modification, not the Phase-0 simulation) and "
            "the death-time forensic captures the real soma file contents — "
            "not just the signed layer fingerprint. Omitted = simulated."
        ),
    )
    p.add_argument(
        "--audit-dir", default="/data/darwin-audit", type=Path,
        help=(
            "Directory for this pod's durable hash-chained audit log "
            "(JSON-lines on disk, NOT the container memory limit). Default "
            "``/data`` is chowned to the pod's uid in the image, so it is "
            "writable and node-disk-backed (the container overlay), not "
            "tmpfs. The genome appends every intent/outcome here; keeping it "
            "on disk instead of the in-memory test sink is what bounds the "
            "pod's memory over an immortal life. Set to 'none' to force the "
            "legacy in-memory sink (bounded sims/tests only)."
        ),
    )
    p.add_argument(
        "--checkpoint-path", default="/data/checkpoint.json", type=Path,
        help="Durable genome checkpoint (on the pod's PVC). 'none' keeps it "
             "in memory (tests).",
    )
    p.add_argument("--heartbeat-seconds", default=5.0, type=float)
    p.add_argument("--host", default="0.0.0.0",
                   help="Bind address for the control API.")
    p.add_argument("--log-level", default="info")
    p.add_argument(
        "--platform-key-path",
        default="/run/secrets/darwin-control/platform_public_key.pem", type=Path,
        help="Platform signing public key (PEM) to verify the DNA against.",
    )
    p.add_argument(
        "--substrate-path",
        default="/run/secrets/darwin-control/substrate.json", type=Path,
        help="Supervisor-forwarded substrate settings (JSON from the control "
             "Secret): LLM, browser, sandbox, watch wallet.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        import uvicorn  # type: ignore[import-not-found]
    except ImportError:
        print(
            "uvicorn is required to run the orchestrator. "
            "Install with: pip install 'darwin-dna[platform]' uvicorn",
            file=sys.stderr,
        )
        return 2

    token = args.control_token_path.read_text().strip()
    if not token:
        print(
            f"control token at {args.control_token_path} is empty",
            file=sys.stderr,
        )
        return 2

    manifest_json, layer_blobs, layer_names = _load_manifest_and_blobs(
        args.manifest_path, args.layer_blob_dir, args.layer_names_path,
    )

    escalation_auto = (
        None if args.escalation_auto == "none" else args.escalation_auto
    )

    clock = time.time
    stack = _build_stack(
        darwin_id=args.darwin_id,
        opening_usdc=args.opening_usdc,
        escalation_auto=escalation_auto,
        manifest_json=manifest_json,
        layer_blobs=layer_blobs,
        layer_names=layer_names,
        clock=clock,
        platform_public_key_pem=(
            args.platform_key_path.read_bytes()
            if args.platform_key_path.exists() else None
        ),
    )
    money_capabilities = _apply_real_adapters(
        stack, _pod_args(args.substrate_path), clock, darwin_id=args.darwin_id,
    )

    # Durable disk-backed audit sink (the prod ``AuditSink`` is "an append-only
    # Postgres table"; the in-memory fake pins the genome's entire hash-chained
    # history in RAM — the pod leak). Must be set BEFORE ``platform()`` captures
    # it. Records live on the node-backed volume, off the container memory
    # limit; the per-heartbeat path only appends, and the chain is streamed on
    # the rare full-scan paths (boot verify, daily Merkle anchor).
    audit_dir = getattr(args, "audit_dir", None)
    if audit_dir is not None and str(audit_dir).lower() != "none":
        from .audit_sink import JsonlAuditSink

        audit_path = Path(audit_dir) / f"{args.darwin_id}.jsonl"
        try:
            stack.audit_sink = JsonlAuditSink(audit_path)
            print(f"audit: durable disk sink at {audit_path}", file=sys.stderr)
        except Exception as exc:  # never block boot — fall back loudly
            print(
                f"warning: disk audit sink at {audit_path} unavailable "
                f"({exc}); falling back to in-memory sink (memory will grow "
                f"with the audit chain)",
                file=sys.stderr,
            )

    # Durable node-boundary checkpoint on the pod's PVC: a restarted pod
    # resumes the same life (tick, profit baseline, open escalations).
    if str(args.checkpoint_path).lower() != "none":
        from .audit_sink import FileCheckpointStore

        stack.checkpoint = FileCheckpointStore(args.checkpoint_path)

    from darwin_dna.evolution import boot
    pod_platform = stack.platform()
    # Money-rails manifest so this pod's planner reasons over the rails it
    # actually has (wallet + x402), mirroring the in-process supervisor path.
    pod_platform.money_capabilities = money_capabilities
    # Soma activation (doc 33): inject this Darwin's evolved soma strategy on
    # the platform BEFORE boot binds it, so a restarted/respawned pod RUNS its
    # promoted self-modifications (the soma tree is PVC-persisted). No-op +
    # fail-closed without a soma root.
    _load_pod_soma_strategy(pod_platform, args.soma_root, args.darwin_id)
    runtime = boot(pod_platform, sleep=lambda _: None)

    # Real soma merge (docs 03/21): when a soma root is configured, install a
    # per-Darwin SomaMerger on the genome's ctx so apply_mutation writes
    # promoted patches to disk (the in-process supervisor does the same via
    # _install_soma_merge_hook). Without it the genome keeps the Phase-0
    # simulation. The merger also feeds the death-time forensic so the k8s
    # capture carries real soma contents, not just the layer fingerprint.
    soma_merger = _install_pod_soma_merge(runtime, args.soma_root, args.darwin_id, clock)

    state = OrchestratorState(
        darwin_id=args.darwin_id,
        stack=stack,
        runtime=runtime,
        clock=clock,
        heartbeat_seconds=args.heartbeat_seconds,
        soma_merger=soma_merger,
    )
    app = _build_control_app(state, token)

    # Start the drive loop AFTER uvicorn brings the event loop up. The
    # FastAPI lifespan "startup" hook is the right place.
    @app.on_event("startup")
    async def _startup() -> None:
        state.start()

    uvicorn.run(
        app, host=args.host, port=args.control_port, log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
