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
    # the env var fallback set is the same.
    from . import __main__ as cli_main
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
    if "browser_factory" in factories:
        stack.browser = factories["browser_factory"](stack.wallet)
    if "x402_factory" in factories:
        stack.x402_client = factories["x402_factory"](stack.wallet)
    if "sandbox_factory" in factories:
        stack.sandbox = factories["sandbox_factory"](stack.wallet)


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
    ) -> None:
        self.darwin_id = darwin_id
        self.stack = stack
        self.runtime = runtime
        self._clock = clock
        self._heartbeat = float(heartbeat_seconds)
        self._task: asyncio.Task | None = None
        self._kill_requested = False
        self._reproduce_requested = False
        self._reproduce_amendment: tuple[bytes, str, str | None] | None = None
        self._closed = False

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
        return {
            "darwin_id": self.darwin_id,
            "alive": bool(st.alive),
            "paused": bool(getattr(st, "paused", False)),
            "killed": bool(self._kill_requested or not st.alive),
            "tick": int(st.tick),
            "days_elapsed": float(st.days_elapsed),
            "balance_usdc": balance,
            "death_reason": st.death_reason,
        }

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

    state = OrchestratorState(
        darwin_id=args.darwin_id,
        stack=stack,
        runtime=runtime,
        clock=clock,
        heartbeat_seconds=args.heartbeat_seconds,
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
