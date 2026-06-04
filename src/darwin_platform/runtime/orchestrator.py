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
import base64
import hashlib
import json
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
        stack.layer_blobs = layer_blobs
        stack.layer_names = layer_names
        # Force boot() to reconstruct identity from the manifest, not
        # the throwaway one FakeStack just minted.
        stack.identity = None
    return stack


def _apply_real_adapters(
    stack, args, clock: Callable[[], float], *, darwin_id: str,
) -> None:
    """Mirror of darwin_platform.__main__._build_real_factories, applied
    directly to a FakeStack. Each adapter is only swapped in when its
    required flag/env is present; otherwise the fake stays."""
    # We intentionally avoid importing __main__ to keep startup light;
    # the env var fallback set is the same. (Note: __main__ lives at the
    # darwin_platform package root, not under runtime/ — relative import
    # needs to go one level up.)
    from .. import __main__ as cli_main
    fake_args = argparse.Namespace(
        # wallet — provider + per-chain config (forwarded from the
        # supervisor's --wallet-provider choice when it launched this pod).
        wallet_provider=args.wallet_provider,
        cdp_api_key_id=getattr(args, "cdp_api_key_id", None),
        cdp_api_key_secret=getattr(args, "cdp_api_key_secret", None),
        cdp_wallet_secret=getattr(args, "cdp_wallet_secret", None),
        cdp_mainnet=getattr(args, "cdp_mainnet", False),
        rpc_url=args.rpc_url, wallet_key=args.wallet_key,
        usdc_contract=args.usdc_contract,
        chain_networks=args.chain_networks,
        solana_rpc_url=args.solana_rpc_url,
        solana_wallet_key=args.solana_wallet_key,
        stellar_horizon_url=args.stellar_horizon_url,
        stellar_secret=args.stellar_secret,
        # vault
        vault_url=args.vault_url, vault_token=args.vault_token,
        vault_mount=args.vault_mount,
        # browser
        browserbase_url=args.browserbase_url,
        browserbase_api_key=args.browserbase_api_key,
        # sandbox
        e2b_api_key=args.e2b_api_key,
    )
    factories = cli_main._build_real_factories(fake_args)  # noqa: SLF001
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
        # Death-time code fingerprint (doc 05), captured once when the genome
        # dies. The supervisor pulls it via GET /forensics before reaping the
        # pod and persists it to its own forensics store.
        self._death_forensics: dict[str, Any] | None = None

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
                await self.runtime.run(max_heartbeats=1)
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
        }

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

    def events_records(self) -> list[dict[str, Any]]:
        """Raw audit-log records held in this pod.

        In ``--driver=k8s`` the Darwin runs here, so its hash-chained audit
        log lives in this pod's ``audit_sink`` — not in the supervisor. The
        supervisor fetches these via the control API and applies all
        filtering/paging itself (single source of truth), so we just hand
        over the records.
        """
        try:
            return self.stack.audit_sink.read_all()
        except Exception:
            return []

    def pause(self, reason: str) -> None:
        self.stack.pause.pause(reason)

    def resume(self) -> None:
        self.stack.pause.resume()

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
    async def force_reproduce(
        body: dict = Body(default_factory=dict),
        _: None = Depends(_require_token),
    ) -> dict:
        amendment_blob = None
        encoded = body.get("amendment_blob_b64")
        if encoded:
            try:
                amendment_blob = base64.b64decode(encoded)
            except Exception:
                raise HTTPException(
                    status_code=400, detail="amendment_blob_b64 is not base64",
                )
        state.force_reproduce(
            amendment_blob=amendment_blob,
            amendment_name=body.get("amendment_name"),
            kill_parent_reason=body.get("kill_parent_reason"),
        )
        return {"requested": True}

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
        "--escalation-auto", default="approve",
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
    p.add_argument("--heartbeat-seconds", default=0.005, type=float)
    p.add_argument("--host", default="0.0.0.0",
                   help="Bind address for the control API.")
    p.add_argument("--log-level", default="info")
    # Real-adapter flags (mirror of __main__.py). Kept here so the
    # orchestrator can talk to real LiteLLM/Vault/Browserbase/E2B without
    # routing through the supervisor.
    p.add_argument("--rpc-url", default=None)
    p.add_argument("--wallet-key", default=None)
    p.add_argument("--usdc-contract", default=None)
    p.add_argument("--chain-networks", default="base")
    p.add_argument("--solana-rpc-url", default=None)
    p.add_argument("--solana-wallet-key", default=None)
    p.add_argument("--stellar-horizon-url", default=None)
    p.add_argument("--stellar-secret", default=None)
    # Wallet provider — supervisor forwards its --wallet-provider choice
    # when launching this pod so each Darwin uses the platform-selected
    # backend (cdp / local-vault / web3). Default matches the supervisor
    # CLI's default.
    p.add_argument(
        "--wallet-provider", default="cdp",
        choices=["web3", "cdp", "local-vault"],
    )
    p.add_argument("--cdp-api-key-id", default=None)
    p.add_argument("--cdp-api-key-secret", default=None)
    p.add_argument("--cdp-wallet-secret", default=None)
    p.add_argument("--cdp-mainnet", action="store_true")
    p.add_argument("--vault-url", default=None)
    p.add_argument("--vault-token", default=None)
    p.add_argument("--vault-mount", default="secret")
    p.add_argument("--browserbase-url", default=None)
    p.add_argument("--browserbase-api-key", default=None)
    p.add_argument("--e2b-api-key", default=None)
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
    )
    _apply_real_adapters(stack, args, clock, darwin_id=args.darwin_id)

    from darwin_dna.evolution import boot
    runtime = boot(stack.platform(), sleep=lambda _: None)

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
