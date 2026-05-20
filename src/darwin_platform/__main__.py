"""``python -m darwin_platform`` — CLI entry point.

Subcommands:

  ``serve``        Run the FastAPI supervisor (uvicorn).
  ``signing-key``  Print the supervisor's signing public key (PEM).

The ``serve`` subcommand wires the optional ``--db`` flag through to the
supervisor's persistence_path so a single command brings up a
restart-survivable platform without a separate config file. Auth is OFF
by default (the surface trusts every caller) — opt in by providing an
``--auth-tokens`` JSON file describing tokens + scopes + owned darwins.

Real-adapter flags (all optional; fakes are used when omitted):

  --rpc-url / DARWIN_RPC_URL         EVM HTTP RPC (Base Sepolia or mainnet)
  --wallet-key / DARWIN_WALLET_KEY   Hex private key for on-chain signing
  --usdc-contract                    USDC ERC-20 address (defaults: Base constants)
  --vault-url / DARWIN_VAULT_URL     HashiCorp Vault address
  --vault-token / DARWIN_VAULT_TOKEN Vault token
  --vault-mount                      KV mount point (default: secret)
  --browserbase-url                  Browserbase WS endpoint (env BROWSERBASE_URL)
  --browserbase-api-key              Browserbase API key (env BROWSERBASE_API_KEY)

Examples::

    python -m darwin_platform serve
    python -m darwin_platform serve --host 0.0.0.0 --port 8000 --db state.db
    python -m darwin_platform serve --auth-tokens tokens.json

    # Run against Base Sepolia testnet with a real Vault:
    python -m darwin_platform serve --db postgres://... \\
        --rpc-url https://sepolia.base.org \\
        --wallet-key 0xdeadbeef... \\
        --vault-url https://vault.example.com:8200 --vault-token s.abc

The tokens.json shape is a list of objects, one per token::

    [
      {"token": "alice-tok", "scopes": ["owner"], "subject": "alice@x.com",
       "owned_darwins": []},
      {"token": "ops-tok",   "scopes": ["platform_operator"], "subject": "ops"}
    ]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _build_real_factories(args: argparse.Namespace) -> dict:
    """Build the optional real-provider factory kwargs for the Supervisor.

    Each factory is only constructed when its required CLI flag (or env
    fallback) is present; otherwise we return None so the Supervisor keeps
    its default FakeLLM / FakeWallet / etc.  All imports are deferred so a
    missing optional extra raises a clear error only when that path is
    actually used.
    """
    factories: dict = {}

    # -- wallet (doc 17: Web3ChainClient → RealWallet) ----------------------
    rpc_url = args.rpc_url or os.environ.get("DARWIN_RPC_URL")
    wallet_key = args.wallet_key or os.environ.get("DARWIN_WALLET_KEY")
    if rpc_url and wallet_key:
        usdc = args.usdc_contract  # None → Web3ChainClient uses its default
        def _wallet_factory(clock, *, _rpc=rpc_url, _key=wallet_key, _usdc=usdc):
            from .adapters.web3_chain import Web3ChainClient
            from .adapters.wallet import RealWallet
            try:
                from web3 import Web3  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "Real wallet requires the [web3] extra: "
                    "pip install 'darwin-dna[web3]'"
                ) from exc
            kwargs = dict(rpc_url=_rpc, private_key=_key)
            if _usdc:
                kwargs["usdc_contract"] = _usdc
            chain = Web3ChainClient(**kwargs)
            return RealWallet(chain=chain, clock=clock)
        factories["wallet_factory"] = _wallet_factory

    # -- vault (doc 16: HvacVaultClient → RealVaultBackend) -----------------
    vault_url = args.vault_url or os.environ.get("DARWIN_VAULT_URL")
    vault_token = args.vault_token or os.environ.get("DARWIN_VAULT_TOKEN")
    if vault_url and vault_token:
        mount = args.vault_mount or "secret"
        def _vault_factory(*, _url=vault_url, _tok=vault_token, _mnt=mount):
            from .adapters.hvac_vault import HvacVaultClient
            from .adapters.vault_backend import RealVaultBackend
            try:
                import hvac  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "Real vault requires the [hvac] extra: "
                    "pip install 'darwin-dna[hvac]'"
                ) from exc
            client = HvacVaultClient(url=_url, token=_tok, mount_point=_mnt)
            return RealVaultBackend(client=client)
        factories["vault_factory"] = _vault_factory

    # -- browser (doc 15: BrowserUseClient on Browserbase) ------------------
    bb_url = args.browserbase_url or os.environ.get("BROWSERBASE_URL")
    bb_key = args.browserbase_api_key or os.environ.get("BROWSERBASE_API_KEY")
    if bb_url or bb_key:
        def _browser_factory(wallet, *, _url=bb_url, _key=bb_key):
            from .adapters.browser import BrowserUseClient
            try:
                import browser_use  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "Real browser requires the [browser] extra: "
                    "pip install 'darwin-dna[browser]'"
                ) from exc
            def _agent_factory(*, url=_url, key=_key):
                import browser_use as bu
                if key:
                    return bu.Agent(browserbase_api_key=key)
                return bu.Agent(browserbase_ws_endpoint=url)
            return BrowserUseClient(wallet=wallet, browser_factory=_agent_factory)
        factories["browser_factory"] = _browser_factory

    # -- x402 (doc 17: X402PaymentClient wrapping httpx) --------------------
    # Wire an x402 client whenever we have a real wallet — the client just
    # needs an HTTP client (httpx) and the wallet for payment settlement.
    # httpx is already on the [platform] extra allowlist.
    if "wallet_factory" in factories:
        def _x402_factory(wallet):
            from .adapters.x402 import X402PaymentClient
            try:
                import httpx
            except ImportError as exc:
                raise ImportError(
                    "x402 client requires httpx: "
                    "pip install 'darwin-dna[platform]'"
                ) from exc
            return X402PaymentClient(http=httpx.Client(), wallet=wallet)
        factories["x402_factory"] = _x402_factory

    return factories


def _serve(args: argparse.Namespace) -> int:
    """Run the FastAPI app via uvicorn. Optional persistence + auth wiring."""
    try:
        import uvicorn  # type: ignore[import-not-found]
    except ImportError:
        print(
            "uvicorn is required to serve. Install with:\n"
            "  pip install 'darwin-dna[platform]' uvicorn",
            file=sys.stderr,
        )
        return 2

    from .api import build_app
    from .auth import InMemoryTokenStore
    from .leadership import PostgresAdvisoryLockElection
    from .supervisor import Supervisor

    leader_election = None
    if args.leader_election:
        if not (args.db and args.db.startswith(("postgres://", "postgresql://"))):
            print(
                "--leader-election requires --db to be a postgres:// DSN",
                file=sys.stderr,
            )
            return 2
        kwargs = {"dsn": args.db}
        if args.leader_lock_key:
            kwargs["lock_key"] = int(args.leader_lock_key)
        leader_election = PostgresAdvisoryLockElection(**kwargs)

    real_factories = _build_real_factories(args)

    sup = Supervisor(
        persistence_path=args.db if args.db else None,
        leader_election=leader_election,
        **real_factories,
    )

    token_store = None
    if args.auth_tokens:
        path = Path(args.auth_tokens).expanduser()
        try:
            tokens = json.loads(path.read_text())
        except Exception as exc:  # pragma: no cover - input validation
            print(f"failed to read {path}: {exc}", file=sys.stderr)
            return 2
        store = sup.persistent_token_store or InMemoryTokenStore()
        for row in tokens:
            store.issue(
                row["token"],
                scopes=set(row.get("scopes", [])),
                owned_darwins=set(row.get("owned_darwins", []) or []),
                subject=row.get("subject"),
            )
        token_store = store

    app = build_app(sup, token_store=token_store)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _signing_key(args: argparse.Namespace) -> int:
    """Print the platform's signing public key. Useful for ops verification
    that a manifest was signed by the right HSM-equivalent identity."""
    from .supervisor import Supervisor

    sup = Supervisor(persistence_path=args.db if args.db else None)
    print(sup.signing_public_key_pem().decode("ascii"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="darwin-platform",
        description=(
            "Darwin platform control plane CLI (docs/engineering/08). "
            "Runs the supervisor + FastAPI surface; optional SQLite "
            "persistence so audit log, billing, lineage, tickets, and "
            "alerts survive restarts."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run the FastAPI supervisor")
    serve.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address (default: 127.0.0.1; set 0.0.0.0 for k8s)",
    )
    serve.add_argument(
        "--port", default=8000, type=int, help="Bind port (default: 8000)",
    )
    serve.add_argument(
        "--db", default=None,
        help="SQLite file for restart-survivable state. Omitted = in-memory.",
    )
    serve.add_argument(
        "--auth-tokens", default=None,
        help=(
            "JSON file of bearer tokens to seed the token store. "
            "Without this, the API trusts every caller — DO NOT expose "
            "publicly."
        ),
    )
    serve.add_argument(
        "--log-level", default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    serve.add_argument(
        "--leader-election", action="store_true",
        help=(
            "Enable HA leader election via Postgres advisory locks. "
            "Requires --db to be a postgres:// DSN. Exactly one pod "
            "holds the lock and drives Darwins; others stay warm + "
            "report /readyz=503 until they win."
        ),
    )
    serve.add_argument(
        "--leader-lock-key", default=None,
        help="Override the default lock key (BIGINT). Used to isolate "
        "platform releases sharing one Postgres instance.",
    )

    # -- real provider flags (docs 14-17) ------------------------------------
    # All optional; fakes are used when omitted. Env-var fallbacks documented
    # in each help string so operators can pass secrets via the chart's
    # envFrom/secretKeyRef without repeating them on the command line.
    wallet_group = serve.add_argument_group(
        "real wallet (doc 17)",
        "Connect to a real EVM chain. Requires the [web3] extra. "
        "When omitted the in-memory FakeWallet is used.",
    )
    wallet_group.add_argument(
        "--rpc-url", default=None, metavar="URL",
        help="EVM HTTP RPC endpoint (e.g. https://sepolia.base.org). "
             "Env: DARWIN_RPC_URL",
    )
    wallet_group.add_argument(
        "--wallet-key", default=None, metavar="HEX",
        help="Hex private key for on-chain USDC signing. "
             "Env: DARWIN_WALLET_KEY. Never log this value.",
    )
    wallet_group.add_argument(
        "--usdc-contract", default=None, metavar="ADDR",
        help="USDC ERC-20 contract address. Defaults to Base Sepolia or "
             "mainnet constant based on the RPC URL.",
    )

    vault_group = serve.add_argument_group(
        "real vault (doc 16)",
        "Connect to HashiCorp Vault. Requires the [hvac] extra. "
        "When omitted the in-memory FakeVault is used.",
    )
    vault_group.add_argument(
        "--vault-url", default=None, metavar="URL",
        help="HashiCorp Vault address (e.g. https://vault.example.com:8200). "
             "Env: DARWIN_VAULT_URL",
    )
    vault_group.add_argument(
        "--vault-token", default=None, metavar="TOKEN",
        help="Vault token with read+write on the KV mount. "
             "Env: DARWIN_VAULT_TOKEN",
    )
    vault_group.add_argument(
        "--vault-mount", default="secret", metavar="MOUNT",
        help="KV v2 mount point (default: secret).",
    )

    browser_group = serve.add_argument_group(
        "real browser (doc 15)",
        "Connect to Browserbase. Requires the [browser] extra. "
        "When omitted the in-memory FakeBrowser is used.",
    )
    browser_group.add_argument(
        "--browserbase-url", default=None, metavar="URL",
        help="Browserbase WS endpoint. Env: BROWSERBASE_URL",
    )
    browser_group.add_argument(
        "--browserbase-api-key", default=None, metavar="KEY",
        help="Browserbase API key. Env: BROWSERBASE_API_KEY",
    )

    serve.set_defaults(func=_serve)

    keycmd = sub.add_parser(
        "signing-key", help="Print the platform's signing public key (PEM)",
    )
    keycmd.add_argument(
        "--db", default=None,
        help="Optional SQLite file (only used so the supervisor opens "
        "consistently; the signing key is per-process)",
    )
    keycmd.set_defaults(func=_signing_key)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
