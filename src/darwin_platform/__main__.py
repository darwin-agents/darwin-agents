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
  --e2b-api-key / E2B_API_KEY        E2B API key for the sandbox adapter
  --sanctions-denylist               JSON file with {denylist: [...], denied_prefixes: [...]}

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
from typing import Any


def _is_mainnet(args: argparse.Namespace) -> bool:
    """Single authoritative answer to 'are we on mainnet?'.

    ``--network mainnet`` is the explicit switch; ``--cdp-mainnet`` is kept as
    a back-compat alias. Everything that needs the network (CDP wallet, USDC
    contract, omnibus, the money-safety gate) keys off THIS — never off an
    ad-hoc RPC-substring guess, so the network can't silently diverge between
    subsystems."""
    return (
        getattr(args, "network", "testnet") == "mainnet"
        or bool(getattr(args, "cdp_mainnet", False))
    )


def _build_real_factories(args: argparse.Namespace) -> dict:
    """Build the optional real-provider factory kwargs for the Supervisor.

    Each factory is only constructed when its required CLI flag (or env
    fallback) is present; otherwise we return None so the Supervisor keeps
    its default FakeLLM / FakeWallet / etc.  All imports are deferred so a
    missing optional extra raises a clear error only when that path is
    actually used.
    """
    factories: dict = {}

    # -- wallet provider: cdp (default), local-vault, or web3 --------------
    # CDP is a complete wallet — when selected it provides wallet_factory
    # and the multi-chain web3 block below is skipped. The argparse layer
    # defaults to "cdp"; the "web3" fallback here is for internal callers
    # (orchestrator pod, tests) that build a Namespace by hand and keep the
    # legacy shared-key behaviour. Don't change this fallback without also
    # giving the orchestrator a --wallet-provider flag to forward.
    wallet_provider = getattr(args, "wallet_provider", "web3")
    if wallet_provider == "cdp":
        cdp_id = getattr(args, "cdp_api_key_id", None) or os.environ.get(
            "CDP_API_KEY_ID"
        )
        cdp_secret = getattr(
            args, "cdp_api_key_secret", None
        ) or os.environ.get("CDP_API_KEY_SECRET")
        cdp_wallet_secret = getattr(
            args, "cdp_wallet_secret", None
        ) or os.environ.get("CDP_WALLET_SECRET")
        if not (cdp_id and cdp_secret):
            print(
                "--wallet-provider cdp requires --cdp-api-key-id + "
                "--cdp-api-key-secret (or the CDP_API_KEY_ID / "
                "CDP_API_KEY_SECRET env vars)",
                file=sys.stderr,
            )
            sys.exit(2)
        cdp_network = (
            "base-mainnet" if _is_mainnet(args) else "base-sepolia"
        )

        def _cdp_wallet_factory(
            darwin_id, clock, vault, *, _id=cdp_id, _secret=cdp_secret,
            _ws=cdp_wallet_secret, _net=cdp_network,
        ):
            # vault is unused — CDP holds the key in its TEE; nothing to seal.
            from .adapters.coinbase_cdp import build_cdp_chain_client
            from .adapters.wallet import RealWallet

            # Per-Darwin CDP account: each spawn gets its own TEE-held wallet.
            # ``get_or_create_account`` is idempotent on the name, so resume
            # re-binds to the same on-chain account.
            chain = build_cdp_chain_client(
                api_key_id=_id, api_key_secret=_secret,
                wallet_secret=_ws, network=_net,
                account_name=f"darwin-{darwin_id}",
            )
            return RealWallet(chain=chain, clock=clock, network=_net)

        factories["wallet_factory"] = _cdp_wallet_factory

    # -- wallet (doc 17: Web3ChainClient → RealWallet) ----------------------
    # Multi-chain mode: --chain-networks lists the chains to enable.
    # When more than one is configured, the wallet uses MultiChainRouter
    # so an outbound transfer routes to the chain that matches the
    # recipient (or falls through the preferred order on failure).
    chain_networks = (args.chain_networks or "base").split(",")
    chain_networks = [n.strip().lower() for n in chain_networks if n.strip()]
    rpc_url = args.rpc_url or os.environ.get("DARWIN_RPC_URL")
    wallet_key = args.wallet_key or os.environ.get("DARWIN_WALLET_KEY")
    solana_rpc = args.solana_rpc_url or os.environ.get("DARWIN_SOLANA_RPC_URL")
    solana_key = args.solana_wallet_key or os.environ.get(
        "DARWIN_SOLANA_WALLET_KEY"
    )
    stellar_horizon = args.stellar_horizon_url or os.environ.get(
        "DARWIN_STELLAR_HORIZON_URL"
    )
    stellar_secret = args.stellar_secret or os.environ.get(
        "DARWIN_STELLAR_SECRET"
    )
    have_base = bool(rpc_url and wallet_key) and "base" in chain_networks
    have_solana = bool(solana_rpc and solana_key) and "solana" in chain_networks
    have_stellar = bool(stellar_horizon and stellar_secret) and "stellar" in chain_networks
    enabled_count = sum([have_base, have_solana, have_stellar])
    if wallet_provider not in ("cdp", "local-vault") and enabled_count > 0:
        # Mainnet refusal: a single shared key is a testnet shortcut.
        # Refuse to start if any configured RPC URL looks like mainnet, so
        # an accidental ``--rpc-url https://mainnet...`` can't sign real
        # money with a fleet-shared key. Operator must opt into cdp or
        # local-vault for mainnet.
        mainnet_hits = [
            (label, url) for label, url in (
                ("--rpc-url", rpc_url if have_base else None),
                ("--solana-rpc-url", solana_rpc if have_solana else None),
                ("--stellar-horizon-url", stellar_horizon if have_stellar else None),
            )
            if url and "mainnet" in url.lower()
        ]
        # horizon.stellar.org is Stellar mainnet too — the substring "mainnet"
        # doesn't appear in the hostname, so check it explicitly.
        if have_stellar and stellar_horizon and (
            "horizon.stellar.org" in stellar_horizon.lower()
        ):
            mainnet_hits.append(("--stellar-horizon-url", stellar_horizon))
        if mainnet_hits:
            for label, url in mainnet_hits:
                print(
                    f"--wallet-provider web3 refuses mainnet RPC {label}={url} "
                    f"— a shared key on mainnet is unsafe. Use "
                    f"--wallet-provider cdp or local-vault instead.",
                    file=sys.stderr,
                )
            sys.exit(2)
        # Shared-key path: a single private key from the platform env signs
        # for every Darwin. ``darwin_id`` and ``vault`` are unused here.
        # The local-vault provider below replaces this with per-Darwin
        # keypairs minted on spawn and sealed in the per-Darwin vault.
        def _wallet_factory(
            darwin_id, clock, vault, *,
            _have_base=have_base, _rpc=rpc_url, _key=wallet_key,
            _usdc=args.usdc_contract,
            _have_solana=have_solana, _solana_rpc=solana_rpc,
            _solana_key=solana_key,
            _have_stellar=have_stellar, _horizon=stellar_horizon,
            _stellar_secret=stellar_secret,
            _preferred=tuple(chain_networks),
        ):
            from .adapters.wallet import RealWallet
            clients: dict = {}
            if _have_base:
                from .adapters.web3_chain import Web3ChainClient
                try:
                    from web3 import Web3  # noqa: F401
                except ImportError as exc:
                    raise ImportError(
                        "Base chain requires the [web3] extra: "
                        "pip install 'darwin-dna[web3]'"
                    ) from exc
                kwargs = dict(rpc_url=_rpc, private_key=_key)
                if _usdc:
                    kwargs["usdc_contract"] = _usdc
                from .adapters.multichain import ChainNetwork
                clients[ChainNetwork.BASE] = Web3ChainClient(**kwargs)
            if _have_solana:
                from .adapters.multichain import ChainNetwork
                from .adapters.solana_sdk import build_solana_chain_client
                cluster = "mainnet" if "mainnet" in (
                    _solana_rpc or ""
                ).lower() else "devnet"
                clients[ChainNetwork.SOLANA] = build_solana_chain_client(
                    rpc_url=_solana_rpc,
                    private_key_hex=_solana_key,
                    cluster=cluster,
                )
            if _have_stellar:
                from .adapters.multichain import ChainNetwork
                from .adapters.stellar_sdk import build_stellar_chain_client
                network = "public" if (
                    "horizon.stellar.org" in (_horizon or "").lower()
                ) else "testnet"
                clients[ChainNetwork.STELLAR] = build_stellar_chain_client(
                    horizon_url=_horizon,
                    secret=_stellar_secret,
                    network=network,
                )
            if len(clients) > 1:
                from .adapters.multichain import (
                    ChainNetwork,
                    MultiChainRouter,
                )
                preferred = [
                    ChainNetwork(n) for n in _preferred
                    if n in ("base", "solana", "stellar")
                    and ChainNetwork(n) in clients
                ]
                chain = MultiChainRouter(
                    clients=clients, preferred=preferred,
                )
            else:
                chain = next(iter(clients.values()))
            return RealWallet(chain=chain, clock=clock)
        factories["wallet_factory"] = _wallet_factory

    # -- wallet (local-vault: per-Darwin minted keypair sealed in vault) ----
    # The factory mints once and reads after — no platform-level secret is
    # required, only the chain's RPC URL. Each Darwin gets an isolated
    # address; reproduction transfers settle to the child's address.
    if wallet_provider == "local-vault":
        local_chains = [
            n for n in chain_networks if n in ("base", "solana", "stellar")
        ]
        if not local_chains:
            print(
                "--wallet-provider local-vault requires --chain-networks "
                "with at least one of base/solana/stellar",
                file=sys.stderr,
            )
            sys.exit(2)
        rpc_map = {"base": rpc_url, "solana": solana_rpc, "stellar": stellar_horizon}
        missing = [c for c in local_chains if not rpc_map.get(c)]
        if missing:
            print(
                f"--wallet-provider local-vault: missing RPC URL for "
                f"{', '.join(missing)}. Set the matching --rpc-url / "
                f"--solana-rpc-url / --stellar-horizon-url (or env var).",
                file=sys.stderr,
            )
            sys.exit(2)

        def _local_vault_wallet_factory(
            darwin_id, clock, vault, *,
            _chains=tuple(local_chains),
            _base_rpc=rpc_url, _usdc=args.usdc_contract,
            _solana_rpc=solana_rpc,
            _stellar_horizon=stellar_horizon,
        ):
            from .adapters.wallet import RealWallet
            from .adapters.wallet_keypair import (
                EvmKeypairMinter,
                SolanaKeypairMinter,
                StellarKeypairMinter,
                ensure_keypair,
            )

            minters = {
                "base": EvmKeypairMinter(),
                "solana": SolanaKeypairMinter(),
                "stellar": StellarKeypairMinter(),
            }

            clients: dict = {}
            for chain_name in _chains:
                kp = ensure_keypair(
                    vault, chain=chain_name, minter=minters[chain_name],
                )
                if chain_name == "base":
                    from .adapters.multichain import ChainNetwork
                    from .adapters.web3_chain import Web3ChainClient
                    kwargs = dict(rpc_url=_base_rpc, private_key=kp.secret)
                    if _usdc:
                        kwargs["usdc_contract"] = _usdc
                    clients[ChainNetwork.BASE] = Web3ChainClient(**kwargs)
                elif chain_name == "solana":
                    from .adapters.multichain import ChainNetwork
                    from .adapters.solana_sdk import build_solana_chain_client
                    cluster = "mainnet" if "mainnet" in (
                        _solana_rpc or ""
                    ).lower() else "devnet"
                    clients[ChainNetwork.SOLANA] = build_solana_chain_client(
                        rpc_url=_solana_rpc,
                        private_key_hex=kp.secret,
                        cluster=cluster,
                    )
                elif chain_name == "stellar":
                    from .adapters.multichain import ChainNetwork
                    from .adapters.stellar_sdk import build_stellar_chain_client
                    network = "public" if (
                        "horizon.stellar.org" in (_stellar_horizon or "").lower()
                    ) else "testnet"
                    clients[ChainNetwork.STELLAR] = build_stellar_chain_client(
                        horizon_url=_stellar_horizon,
                        secret=kp.secret,
                        network=network,
                    )

            if len(clients) > 1:
                from .adapters.multichain import ChainNetwork, MultiChainRouter
                preferred = [
                    ChainNetwork(n) for n in _chains
                    if ChainNetwork(n) in clients
                ]
                chain_client = MultiChainRouter(
                    clients=clients, preferred=preferred,
                )
            else:
                chain_client = next(iter(clients.values()))
            return RealWallet(chain=chain_client, clock=clock)

        factories["wallet_factory"] = _local_vault_wallet_factory

    # -- vault (doc 16: HvacVaultClient → RealVaultBackend) -----------------
    vault_url = args.vault_url or os.environ.get("DARWIN_VAULT_URL")
    vault_token = args.vault_token or os.environ.get("DARWIN_VAULT_TOKEN")
    vault_auth = getattr(args, "vault_auth", None) or "token"
    vault_role_id = getattr(args, "vault_role_id", None) or os.environ.get(
        "DARWIN_VAULT_ROLE_ID"
    )
    vault_secret_id = getattr(args, "vault_secret_id", None) or os.environ.get(
        "DARWIN_VAULT_SECRET_ID"
    )
    vault_k8s_role = getattr(args, "vault_k8s_role", None) or os.environ.get(
        "DARWIN_VAULT_K8S_ROLE"
    )
    vault_k8s_jwt_path = (
        getattr(args, "vault_k8s_jwt_path", None)
        or "/var/run/secrets/kubernetes.io/serviceaccount/token"
    )
    # Ergonomics: if the operator only supplied AppRole / k8s creds (no
    # explicit --vault-auth), pick the matching method automatically.
    if vault_auth == "token":
        if vault_role_id and vault_secret_id:
            vault_auth = "approle"
        elif vault_k8s_role:
            vault_auth = "kubernetes"
    vault_has_auth = (
        (vault_auth == "token" and bool(vault_token))
        or (vault_auth == "approle" and bool(vault_role_id and vault_secret_id))
        or (vault_auth == "kubernetes" and bool(vault_k8s_role))
    )
    if vault_url and vault_has_auth:
        mount = args.vault_mount or "secret"

        def _vault_factory(
            *, _url=vault_url, _tok=vault_token, _mnt=mount, _auth=vault_auth,
            _rid=vault_role_id, _sid=vault_secret_id, _krole=vault_k8s_role,
            _kjwt=vault_k8s_jwt_path,
        ):
            from .adapters.hvac_vault import HvacVaultClient
            from .adapters.vault_backend import RealVaultBackend
            try:
                import hvac  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "Real vault requires the [hvac] extra: "
                    "pip install 'darwin-dna[hvac]'"
                ) from exc
            client = HvacVaultClient(
                url=_url, token=_tok, mount_point=_mnt, auth_method=_auth,
                role_id=_rid, secret_id=_sid, k8s_role=_krole,
                k8s_jwt_path=_kjwt,
            )
            return RealVaultBackend(client=client)
        factories["vault_factory"] = _vault_factory

    # -- browser (docs 15, 31) ---------------------------------------------
    # Two providers: browserbase (OSS browser_use lib + Browserbase runtime)
    # or browser-use-cloud (V3 Agent API). Picked via --browser-provider.
    browser_provider = getattr(args, "browser_provider", "browserbase")
    if browser_provider == "browser-use-cloud":
        bu_key = (
            getattr(args, "browser_use_api_key", None)
            or os.environ.get("BROWSER_USE_API_KEY")
        )
        if not bu_key:
            print(
                "--browser-provider browser-use-cloud requires "
                "--browser-use-api-key or BROWSER_USE_API_KEY",
                file=sys.stderr,
            )
            sys.exit(2)
        bu_model = getattr(args, "browser_use_model", None)
        bu_max_cost = float(getattr(args, "browser_use_max_cost_usdc", 0.10))

        def _browser_factory(
            wallet, *, _key=bu_key, _model=bu_model, _cap=bu_max_cost,
        ):
            from .adapters.browser_use_cloud import BrowserUseCloudClient
            try:
                import browser_use_sdk  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "--browser-provider browser-use-cloud requires the "
                    "[browser-use-cloud] extra: "
                    "pip install 'darwin-dna[browser-use-cloud]'"
                ) from exc
            return BrowserUseCloudClient(
                wallet=wallet,
                api_key=_key,
                model=_model,
                max_cost_usdc=_cap,
            )
        factories["browser_factory"] = _browser_factory
    else:
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

    # -- sandbox (doc 14: E2BSandbox) ---------------------------------------
    e2b_key = args.e2b_api_key or os.environ.get("E2B_API_KEY")
    if e2b_key:
        def _sandbox_factory(wallet, *, _key=e2b_key):
            from .adapters.sandbox import E2BSandbox
            try:
                import e2b  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "Real sandbox requires the [e2b] extra: "
                    "pip install 'darwin-dna[e2b]'"
                ) from exc
            def _session_factory(*, key=_key):
                import e2b as _e2b
                return _e2b.Sandbox(api_key=key)
            return E2BSandbox(wallet=wallet, sandbox_factory=_session_factory)
        factories["sandbox_factory"] = _sandbox_factory

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

    # -- LLM gateway (doc 13: LiteLLMTransport → real proxy) ---------------
    # Point the genome's LLM transport at a running LiteLLM proxy. The
    # bundled docker/litellm.config.yaml exposes tier-* model names on an
    # OpenAI-compatible surface, so `openai/tier-*` + api_base routes
    # every call through the proxy. Without --litellm-url the supervisor
    # keeps the deterministic FakeLLM.
    litellm_url = getattr(args, "litellm_url", None) or os.environ.get(
        "DARWIN_LITELLM_URL"
    )
    if litellm_url:
        master_key = (
            getattr(args, "litellm_master_key", None)
            or os.environ.get("DARWIN_LITELLM_MASTER_KEY")
            or "dev_only_master_key"
        )

        def _llm_factory(wallet, clock, *, _url=litellm_url, _key=master_key):
            import functools

            from .adapters.litellm_transport import LiteLLMTransport, TierMapping
            try:
                import litellm  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "LiteLLM transport requires the [llm] extra: "
                    "pip install 'darwin-dna[llm]'"
                ) from exc
            # Bind the proxy onto the NATIVE-ASYNC client. The transport
            # drives this on an isolated worker-loop and never the
            # supervisor's running loop — the synchronous litellm.completion
            # re-enters the running loop (nest_asyncio) and on uvloop freezes
            # the drive loop after one heartbeat (BUG-drive-loop-tick1-freeze).
            acompletion = functools.partial(
                litellm.acompletion, api_base=_url.rstrip("/"), api_key=_key,
            )
            # image_gen maps onto the fast tier — the bundled proxy config
            # has no image model; the genome's image path is rare and
            # degrades gracefully on a tier mismatch.
            tier_mappings = {
                "fast": TierMapping(models=("openai/tier-fast",)),
                "reasoning": TierMapping(models=("openai/tier-reasoning",)),
                "coding": TierMapping(models=("openai/tier-coding",)),
                "embedding": TierMapping(models=("openai/tier-embedding",)),
                "image_gen": TierMapping(models=("openai/tier-fast",)),
            }
            return LiteLLMTransport(
                wallet=wallet,
                tier_mappings=tier_mappings,
                litellm_acompletion=acompletion,
                litellm_completion_cost=litellm.completion_cost,
            )

        factories["llm_transport_factory"] = _llm_factory

    # LanceDB-backed episodic memory (doc 03, 11). Per-Darwin vector
    # substrate; the genome keeps its deterministic default unless this
    # factory is installed. Each Darwin gets its own LanceDB table at
    # <base>/<darwin_id> so lifetimes stay isolated on disk; embeddings
    # are produced via litellm.embedding (routed through the proxy when
    # --litellm-url is set, direct otherwise).
    episodic_choice = getattr(args, "episodic_memory", "deterministic")
    lancedb_path = getattr(args, "lancedb_path", None) or os.environ.get(
        "DARWIN_LANCEDB_PATH"
    )
    if episodic_choice == "lancedb" and lancedb_path:
        embed_model = (
            getattr(args, "lancedb_embedding_model", None)
            or "openai/text-embedding-3-small"
        )
        litellm_base = getattr(args, "litellm_url", None) or os.environ.get(
            "DARWIN_LITELLM_URL"
        )
        litellm_key = (
            getattr(args, "litellm_master_key", None)
            or os.environ.get("DARWIN_LITELLM_MASTER_KEY")
        )
        try:
            import litellm  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "LanceDB episodic memory needs litellm for embeddings: "
                "pip install 'darwin-dna[llm,lancedb]'"
            ) from exc

        def _embed(
            text: str,
            *,
            _model=embed_model,
            _url=litellm_base,
            _key=litellm_key,
        ) -> list[float]:
            import litellm as _litellm
            kwargs: dict[str, Any] = {"model": _model, "input": text}
            if _url:
                kwargs["api_base"] = _url.rstrip("/")
            if _key:
                kwargs["api_key"] = _key
            resp = _litellm.embedding(**kwargs)
            return list(resp["data"][0]["embedding"])

        def _lancedb_factory(
            darwin_id: str, *, _base=lancedb_path, _embed=_embed,
        ):
            from .adapters.lancedb_memory import LanceDbEpisodicMemory
            db_path = os.path.join(_base, darwin_id)
            os.makedirs(db_path, exist_ok=True)
            return LanceDbEpisodicMemory(embed=_embed, db_path=db_path)

        factories["episodic_memory_factory"] = _lancedb_factory

    return factories


def _build_platform_services(args: argparse.Namespace) -> dict[str, Any]:
    """Construct the Phase 1/2 supervisor-level services from CLI flags.

    Returns a dict of :class:`Supervisor` kwargs. Everything is optional:
    an unset flag leaves the corresponding kwarg out so the supervisor
    keeps its default (in-process Ed25519 signer, no drift detector,
    etc.). All heavy imports are deferred."""
    kwargs: dict[str, Any] = {}

    # -- maintenance cadence (doc 22) ------------------------------------
    kwargs["maintenance_interval_seconds"] = float(args.maintenance_interval)

    # -- HSM-backed DNA signing (doc 21) --------------------------------
    transit_key = getattr(args, "signing_vault_transit_key", None)
    if transit_key:
        vault_url = args.vault_url or os.environ.get("DARWIN_VAULT_URL")
        vault_token = args.vault_token or os.environ.get("DARWIN_VAULT_TOKEN")
        if not (vault_url and vault_token):
            print(
                "--signing-vault-transit-key requires --vault-url + "
                "--vault-token",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            import hvac
        except ImportError as exc:
            raise ImportError(
                "Vault Transit signing requires the [hvac] extra: "
                "pip install 'darwin-dna[hvac]'"
            ) from exc
        from .security.signing_service import build_signer

        client = hvac.Client(url=vault_url, token=vault_token)
        kwargs["signer"] = build_signer(
            hvac_client=client, transit_key_name=transit_key,
        )

    # -- drift detection (doc 22) ----------------------------------------
    if getattr(args, "enable_drift_detection", False):
        from .observability.drift_detector import DriftDetector

        kwargs["drift_detector"] = DriftDetector()

    # -- Merkle anchoring (doc 22) ---------------------------------------
    if getattr(args, "enable_merkle_anchor", False):
        from .observability.merkle_anchor import (
            MerkleAnchorPublisher,
            StubAnchorBackend,
        )

        # Dev/local uses the in-memory anchor backend; a production
        # deployment swaps in ChainAnchorBackend against a funded
        # platform anchor wallet.
        kwargs["merkle_anchor_publisher"] = MerkleAnchorPublisher(
            backend=StubAnchorBackend(),
        )

    # -- omnibus wallet (doc 22) -----------------------------------------
    if getattr(args, "enable_omnibus", False):
        rpc_url = args.rpc_url or os.environ.get("DARWIN_RPC_URL")
        wallet_key = args.wallet_key or os.environ.get("DARWIN_WALLET_KEY")
        if not (rpc_url and wallet_key):
            print(
                "--enable-omnibus requires a real chain "
                "(--rpc-url + --wallet-key)",
                file=sys.stderr,
            )
            sys.exit(2)
        from eth_account import Account
        from web3 import Web3

        from .adapters.web3_chain import Web3ChainClient
        from .payments.omnibus_wallet import OmnibusWallet

        # Match the USDC contract + chain id to the network (--network /
        # --cdp-mainnet). The omnibus address is derived from its signing key.
        is_mainnet = _is_mainnet(args)
        usdc_contract = args.usdc_contract or (
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913" if is_mainnet
            else "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
        )
        chain_id = 8453 if is_mainnet else 84532
        omnibus_address = Account.from_key(wallet_key).address
        kwargs["omnibus_wallet"] = OmnibusWallet(
            Web3ChainClient(
                web3_factory=lambda url=rpc_url: Web3(Web3.HTTPProvider(url)),
                wallet_address=omnibus_address,
                private_key=wallet_key,
                usdc_contract=usdc_contract,
                chain_id=chain_id,
            ),
        )

    # -- KYC (doc 22) ----------------------------------------------------
    kyc_provider = getattr(args, "kyc_provider", "none")
    if kyc_provider == "stub":
        from .security.kyc import KYCService, StubKYCProvider

        kwargs["kyc_service"] = KYCService(provider=StubKYCProvider())
    elif kyc_provider == "persona":
        api_key = getattr(args, "kyc_persona_api_key", None) or os.environ.get(
            "DARWIN_KYC_PERSONA_API_KEY"
        )
        template_id = getattr(
            args, "kyc_persona_template_id", None
        ) or os.environ.get("DARWIN_KYC_PERSONA_TEMPLATE_ID")
        if not (api_key and template_id):
            print(
                "--kyc-provider persona requires --kyc-persona-api-key + "
                "--kyc-persona-template-id",
                file=sys.stderr,
            )
            sys.exit(2)
        from .security.kyc import KYCService, PersonaKYCProvider

        kwargs["kyc_service"] = KYCService(
            provider=PersonaKYCProvider(
                api_key=api_key, template_id=template_id,
            ),
        )
    elif kyc_provider == "onfido":
        api_token = getattr(
            args, "kyc_onfido_api_token", None
        ) or os.environ.get("ONFIDO_API_TOKEN")
        workflow_id = getattr(
            args, "kyc_onfido_workflow_id", None
        ) or os.environ.get("ONFIDO_WORKFLOW_ID")
        if not (api_token and workflow_id):
            print(
                "--kyc-provider onfido requires --kyc-onfido-api-token + "
                "--kyc-onfido-workflow-id (or the ONFIDO_API_TOKEN / "
                "ONFIDO_WORKFLOW_ID env vars)",
                file=sys.stderr,
            )
            sys.exit(2)
        from .security.kyc import KYCService, OnfidoKYCProvider

        kwargs["kyc_service"] = KYCService(
            provider=OnfidoKYCProvider(
                api_token=api_token,
                workflow_id=workflow_id,
                region=getattr(args, "kyc_onfido_region", None) or "eu",
            ),
        )

    # -- fiat ramp (doc 17) ----------------------------------------------
    fiat_provider = getattr(args, "fiat_provider", "none")
    if fiat_provider == "stub":
        from .adapters.fiat import StubFiatRamp

        kwargs["fiat_ramp"] = StubFiatRamp()
    elif fiat_provider == "stripe":
        api_key = getattr(args, "stripe_api_key", None) or os.environ.get(
            "STRIPE_API_KEY"
        )
        if not api_key:
            print(
                "--fiat-provider stripe requires --stripe-api-key "
                "(or the STRIPE_API_KEY env var)",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            import stripe
        except ImportError as exc:
            raise ImportError(
                "Stripe fiat ramp requires the [stripe] extra: "
                "pip install 'darwin-dna[stripe]'"
            ) from exc
        stripe.api_key = api_key
        from .adapters.fiat import StripeFiatRamp

        kwargs["fiat_ramp"] = StripeFiatRamp(stripe_client=stripe)
    elif fiat_provider == "moonpay":
        api_key = getattr(args, "moonpay_api_key", None) or os.environ.get(
            "MOONPAY_API_KEY"
        )
        if not api_key:
            print(
                "--fiat-provider moonpay requires --moonpay-api-key "
                "(or the MOONPAY_API_KEY env var)",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "MoonPay fiat ramp requires httpx: "
                "pip install 'darwin-dna[platform]'"
            ) from exc
        from .adapters.fiat import MoonPayFiatRamp

        kwargs["fiat_ramp"] = MoonPayFiatRamp(
            http_client=httpx.Client(timeout=10.0), api_key=api_key,
        )

    # -- SMS 2FA inbox (doc 15) ------------------------------------------
    twilio_sid = getattr(args, "twilio_account_sid", None) or os.environ.get(
        "TWILIO_ACCOUNT_SID"
    )
    twilio_token = getattr(args, "twilio_auth_token", None) or os.environ.get(
        "TWILIO_AUTH_TOKEN"
    )
    if twilio_sid and twilio_token:
        from .adapters.twilio_sms import TwilioSmsClient

        kwargs["sms_client"] = TwilioSmsClient(
            account_sid=twilio_sid,
            auth_token=twilio_token,
            phone_number=(
                getattr(args, "twilio_phone_number", None)
                or os.environ.get("TWILIO_PHONE_NUMBER")
            ),
        )

    return kwargs


def _build_sanctions(args: argparse.Namespace):
    """Construct the SanctionsScreener from CLI flags.

    Priority order: --sanctions-provider {chainalysis,trm} requires
    --sanctions-api-key; otherwise --sanctions-denylist falls back to
    LocalDenylistScreener; otherwise None (no screening)."""
    provider = getattr(args, "sanctions_provider", None) or "local"
    api_key = getattr(args, "sanctions_api_key", None) or os.environ.get(
        "DARWIN_SANCTIONS_API_KEY"
    )
    if provider == "chainalysis":
        if not api_key:
            print(
                "--sanctions-provider chainalysis requires --sanctions-api-key",
                file=sys.stderr,
            )
            sys.exit(2)
        from .payments.sanctions import CachedScreener, ChainalysisScreener
        return CachedScreener(ChainalysisScreener(api_key=api_key))
    if provider == "trm":
        if not api_key:
            print(
                "--sanctions-provider trm requires --sanctions-api-key",
                file=sys.stderr,
            )
            sys.exit(2)
        from .payments.sanctions import CachedScreener, TRMScreener
        return CachedScreener(TRMScreener(api_key=api_key))
    # provider == "local" (default)
    denylist_path = getattr(args, "sanctions_denylist", None)
    if not denylist_path:
        return None
    from .payments.sanctions import LocalDenylistScreener
    try:
        data = json.loads(Path(denylist_path).expanduser().read_text())
    except Exception as exc:  # pragma: no cover
        print(
            f"failed to read sanctions denylist {denylist_path}: {exc}",
            file=sys.stderr,
        )
        sys.exit(2)
    return LocalDenylistScreener(
        denylist=set(data.get("denylist", [])),
        denied_prefixes=tuple(data.get("denied_prefixes", [])),
    )


def _build_notification_sink(args: argparse.Namespace):
    """Build a notification sink from CLI flags. Multiple flags fan
    out — Slack + webhook + email are all sent if all three are set."""
    slack_url = getattr(args, "slack_webhook_url", None) or os.environ.get(
        "DARWIN_SLACK_WEBHOOK_URL"
    )
    webhook_url = getattr(args, "escalation_webhook_url", None) or os.environ.get(
        "DARWIN_ESCALATION_WEBHOOK_URL"
    )
    sinks: list = []
    if slack_url:
        from .observability.sinks import SlackWebhookNotificationSink
        sinks.append(SlackWebhookNotificationSink(webhook_url=slack_url))
    if webhook_url:
        from .observability.sinks import GenericWebhookNotificationSink
        sinks.append(GenericWebhookNotificationSink(url=webhook_url))
    webpush_sink = _build_webpush_sink(args)
    if webpush_sink is not None:
        sinks.append(webpush_sink)
    if not sinks:
        return None
    if len(sinks) == 1:
        return sinks[0]
    from .observability.sinks import FanOutNotificationSink
    return FanOutNotificationSink(sinks)


def _build_webpush_sink(args: argparse.Namespace):
    """Construct a :class:`WebPushNotificationSink` when the operator has
    opted in via ``--push-provider webpush`` and supplied a VAPID keypair.
    Returns ``None`` otherwise. Refuses to start with half-configured
    VAPID material — a silent no-op would be worse than crashing."""
    if getattr(args, "push_provider", "none") != "webpush":
        return None
    private = getattr(args, "vapid_private_key", None) or os.environ.get(
        "DARWIN_VAPID_PRIVATE_KEY"
    )
    subject = getattr(args, "vapid_subject", None) or os.environ.get(
        "DARWIN_VAPID_SUBJECT"
    )
    if not private or not subject:
        print(
            "--push-provider webpush requires --vapid-private-key and "
            "--vapid-subject (or DARWIN_VAPID_PRIVATE_KEY + "
            "DARWIN_VAPID_SUBJECT). Refusing to start.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    # Allow `--vapid-private-key /path/to/key.pem` as a shorthand for
    # reading the PEM from disk so secrets don't have to ride the command
    # line. The discriminator is "starts with -" (PEM header) or not.
    if not private.startswith("-----"):
        try:
            private = Path(private).expanduser().read_text()
        except Exception as exc:
            print(f"failed to read VAPID private key: {exc}", file=sys.stderr)
            raise SystemExit(2)
    from .escalations.notification_webpush import (
        PyWebPushTransport,
        WebPushNotificationSink,
    )
    return WebPushNotificationSink(
        vapid_private_key=private,
        vapid_subject=subject,
        transport=PyWebPushTransport(),
    )


def _resolve_vapid_public_key(args: argparse.Namespace) -> str | None:
    """The public half of the VAPID keypair, surfaced to the PWA via
    GET /me/push-vapid-public-key. Returned even when no sink is built,
    since the dashboard may want to display "push not configured" with
    a hint."""
    if getattr(args, "push_provider", "none") != "webpush":
        return None
    return (
        getattr(args, "vapid_public_key", None)
        or os.environ.get("DARWIN_VAPID_PUBLIC_KEY")
    )


def _build_alert_sink(args: argparse.Namespace):
    """Build an alert sink. Same fan-out semantics as notifications."""
    webhook_url = getattr(args, "alert_webhook_url", None) or os.environ.get(
        "DARWIN_ALERT_WEBHOOK_URL"
    )
    pd_routing = getattr(args, "pagerduty_routing_key", None) or os.environ.get(
        "DARWIN_PAGERDUTY_ROUTING_KEY"
    )
    sinks: list = []
    if webhook_url:
        from .observability.sinks import GenericWebhookAlertSink
        sinks.append(GenericWebhookAlertSink(url=webhook_url))
    if pd_routing:
        from .observability.sinks import PagerDutyAlertSink
        sinks.append(PagerDutyAlertSink(routing_key=pd_routing))
    if not sinks:
        return None
    if len(sinks) == 1:
        return sinks[0]
    from .observability.sinks import FanOutAlertSink
    return FanOutAlertSink(sinks)


def _build_k8s_driver(args: argparse.Namespace):
    """Build a :class:`darwin_platform.k8s.K8sDarwinDriver` when
    ``--driver=k8s`` is set; otherwise ``None`` (the supervisor uses
    its in-process drive loop)."""
    if getattr(args, "driver", "in-process") != "k8s":
        return None
    from .k8s.driver import make_default_k8s_driver
    from .k8s.manifests import DarwinPodConfig

    orchestrator_image = getattr(args, "k8s_orchestrator_image", None) or (
        "ghcr.io/quantium-rock/darwin-agent/darwin-platform:latest"
    )
    postgres_ns = getattr(args, "k8s_postgres_namespace", None) or ""
    postgres_label = getattr(args, "k8s_postgres_pod_label", None) or "="
    pg_key, _, pg_val = postgres_label.partition("=")
    pod_config = DarwinPodConfig(
        orchestrator_image=orchestrator_image,
        runtime_class_name=getattr(args, "k8s_runtime_class", None) or "kata",
        lancedb_image=getattr(args, "k8s_lancedb_image", None) or "lancedb/lancedb:latest",
        lancedb_storage_size=getattr(args, "k8s_lancedb_storage_size", None) or "1Gi",
        lancedb_storage_class=getattr(args, "k8s_lancedb_storage_class", None) or None,
        platform_data_namespace=(
            getattr(args, "k8s_egress_proxy_namespace", None) or "platform-data"
        ),
        postgres_namespace=postgres_ns,
        postgres_pod_label_key=pg_key,
        postgres_pod_label_value=pg_val,
        postgres_port=int(getattr(args, "k8s_postgres_port", 5432) or 5432),
    )
    database_url_secret = getattr(args, "k8s_database_url_secret", None) or ""
    secret_ref: tuple[str, str] | None = None
    if database_url_secret:
        sname, _, skey = database_url_secret.partition(":")
        secret_ref = (sname, skey or "DATABASE_URL")
    return make_default_k8s_driver(
        pod_config=pod_config,
        namespace_prefix=getattr(args, "k8s_namespace_prefix", None) or "darwin-",
        database_url_secret_ref=secret_ref,
    )


def _assert_mainnet_safe(args: argparse.Namespace) -> None:
    """Refuse to boot against mainnet with demo-grade money-safety config.

    Mirrors the web3 shared-key mainnet refusal: moving REAL funds demands a
    live sanctions screener (an offline denylist can't catch a freshly-listed
    address), and the credit-minting faucet endpoint must be off. Testnet is
    unaffected. The network is the explicit --network / --cdp-mainnet flag
    (see :func:`_is_mainnet`), and we also refuse a network/RPC mismatch so a
    testnet-configured boot can't be pointed at a mainnet RPC by accident."""
    rpc = (
        getattr(args, "rpc_url", None)
        or os.environ.get("DARWIN_RPC_URL")
        or ""
    )
    is_mainnet = _is_mainnet(args)
    # Coherence: an explicit testnet boot pointed at a mainnet RPC is almost
    # certainly a mistake — fail loud rather than sign real-money txns with
    # testnet-grade safety config.
    if not is_mainnet and "mainnet" in rpc.lower():
        print(
            f"refusing to boot: --network testnet but DARWIN_RPC_URL={rpc} "
            "looks like mainnet. Pass --network mainnet (with live compliance "
            "config) or point at a testnet RPC.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not is_mainnet:
        return
    sanctions = getattr(args, "sanctions_provider", None) or "local"
    if sanctions not in ("chainalysis", "trm"):
        print(
            f"refusing to boot on mainnet with sanctions-provider={sanctions!r}: "
            "real-money transfers require a live screener (--sanctions-provider "
            "chainalysis|trm + --sanctions-api-key). An offline denylist is not "
            "acceptable for mainnet.",
            file=sys.stderr,
        )
        sys.exit(2)
    if getattr(args, "allow_manual_credit", False):
        print(
            "refusing to boot on mainnet with --allow-manual-credit: minting "
            "unbacked user credit against real funds is unsafe. In production, "
            "credit only via verified Stripe/on-chain deposits.",
            file=sys.stderr,
        )
        sys.exit(2)


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
    from .runtime.leadership import PostgresAdvisoryLockElection
    from .runtime.supervisor import Supervisor
    from .security.auth import InMemoryTokenStore

    # Mainnet money-safety gate (refuses demo-grade config against real funds).
    _assert_mainnet_safe(args)

    # -- OpenTelemetry trace export (doc 08) -----------------------------
    # Wire a real OTLP exporter so the spans the supervisor emits leave
    # the process. Best-effort: a missing exporter package warns rather
    # than aborting serve (tracing falls back to NoOp).
    otlp_endpoint = args.otlp_endpoint or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if otlp_endpoint:
        from .observability.tracing import configure_otlp_tracing

        try:
            if configure_otlp_tracing(otlp_endpoint):
                print(f"tracing: exporting spans to {otlp_endpoint}")
        except ImportError as exc:
            print(f"tracing: OTLP export unavailable ({exc})", file=sys.stderr)

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

    sanctions = _build_sanctions(args)
    notification_sink = _build_notification_sink(args)
    alert_sink = _build_alert_sink(args)

    k8s_driver = _build_k8s_driver(args)
    if k8s_driver is not None and real_factories:
        # The K8s driver hosts every per-Darwin substrate inside the
        # orchestrator pod, not in the supervisor process. Passing
        # in-process factories alongside k8s mode would silently apply
        # them to a code path that never spawns Darwins — refuse clearly.
        print(
            "real-adapter flags (--rpc-url etc.) are not supported with "
            "--driver=k8s; pass those to the orchestrator image instead",
            file=sys.stderr,
        )
        return 2

    supervisor_kwargs: dict[str, Any] = {}
    if args.heartbeat_seconds is not None:
        supervisor_kwargs["heartbeat_seconds"] = float(args.heartbeat_seconds)
    # Phase 1/2 supervisor-level services (signer, drift, merkle, omnibus,
    # KYC, maintenance cadence). Supervisor-side — applied in both
    # in-process and k8s mode; only the per-Darwin substrate factories
    # are k8s-excluded.
    supervisor_kwargs.update(_build_platform_services(args))
    sup = Supervisor(
        persistence_path=args.db if args.db else None,
        leader_election=leader_election,
        sanctions_screener=sanctions,
        notification_sink=notification_sink,
        alert_sink=alert_sink,
        push_vapid_public_key=_resolve_vapid_public_key(args),
        k8s_driver=k8s_driver,
        **supervisor_kwargs,
        **(real_factories if k8s_driver is None else {}),
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

    # Clerk auth — an alternative to --auth-tokens. When a Clerk issuer or
    # JWKS URL is configured, bearer tokens are verified as Clerk JWTs.
    if token_store is None:
        clerk_issuer = getattr(args, "clerk_issuer", None) or os.environ.get(
            "CLERK_ISSUER"
        )
        clerk_jwks_url = getattr(
            args, "clerk_jwks_url", None
        ) or os.environ.get("CLERK_JWKS_URL")
        if clerk_issuer or clerk_jwks_url:
            from .security.clerk import ClerkTokenStore, ClerkVerifier

            verifier = ClerkVerifier(
                jwks_url=clerk_jwks_url,
                issuer=clerk_issuer,
                audience=(
                    getattr(args, "clerk_audience", None)
                    or os.environ.get("CLERK_AUDIENCE")
                ),
            )
            token_store = ClerkTokenStore(verifier=verifier)

    app = build_app(
        sup,
        token_store=token_store,
        cors_origins=list(getattr(args, "cors_origins", None) or []),
        stripe_webhook_secret=(
            getattr(args, "stripe_webhook_secret", None)
            or os.environ.get("STRIPE_WEBHOOK_SECRET")
        ),
        allow_manual_credit=bool(getattr(args, "allow_manual_credit", False)),
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _signing_key(args: argparse.Namespace) -> int:
    """Print the platform's signing public key. Useful for ops verification
    that a manifest was signed by the right HSM-equivalent identity."""
    from .runtime.supervisor import Supervisor

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
        "--heartbeat-seconds", default=5.0, type=float,
        help=(
            "Gap between per-Darwin heartbeats (default: 5.0). The supervisor "
            "library default is 0.001 (tuned for fast tests); ``serve`` overrides "
            "to a production-shaped pace so the audit chain doesn't bloat by "
            "millions of records per hour and the spread-spend monitor's "
            "production thresholds make sense in a sim. Drop to 0.001 for a "
            "soak; raise to 60+ to match a slower real-world cadence."
        ),
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
        "--cors-origin", dest="cors_origins", action="append", default=[],
        metavar="ORIGIN",
        help=(
            "Allow this origin to make CORS requests. Repeat for multiple "
            "(e.g. --cors-origin http://localhost:3000 --cors-origin https://app.example.com). "
            "Required when the Next.js dashboard runs on a separate port; "
            "without it the browser preflight OPTIONS gets a 405. "
            "Defaults to empty — same-origin only."
        ),
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

    clerk_group = serve.add_argument_group(
        "Clerk auth (doc 08)",
        "Verify control-plane bearer tokens as Clerk-issued RS256 JWTs "
        "against Clerk's JWKS. An alternative to --auth-tokens; both are "
        "optional (no auth configured = trust every caller — dev only).",
    )
    clerk_group.add_argument(
        "--clerk-issuer", default=None, metavar="URL",
        help="Clerk issuer URL — the JWKS is derived as "
             "<issuer>/.well-known/jwks.json. Env: CLERK_ISSUER",
    )
    clerk_group.add_argument(
        "--clerk-jwks-url", default=None, metavar="URL",
        help="Clerk JWKS URL (overrides the issuer-derived default). "
             "Env: CLERK_JWKS_URL",
    )
    clerk_group.add_argument(
        "--clerk-audience", default=None, metavar="AUD",
        help="Expected JWT 'aud' claim (optional). Env: CLERK_AUDIENCE",
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
    wallet_group.add_argument(
        "--chain-networks", default="base", metavar="LIST",
        help=(
            "Comma-separated list of chains to enable, in preferred "
            "order: base, solana, stellar. Default: base only. With "
            "multiple chains, transfers route via MultiChainRouter."
        ),
    )
    wallet_group.add_argument(
        "--solana-rpc-url", default=None, metavar="URL",
        help="Solana HTTP RPC endpoint. Env: DARWIN_SOLANA_RPC_URL",
    )
    wallet_group.add_argument(
        "--solana-wallet-key", default=None, metavar="HEX",
        help="Solana wallet private key. Env: DARWIN_SOLANA_WALLET_KEY",
    )
    wallet_group.add_argument(
        "--stellar-horizon-url", default=None, metavar="URL",
        help="Stellar Horizon RPC endpoint. Env: DARWIN_STELLAR_HORIZON_URL",
    )
    wallet_group.add_argument(
        "--stellar-secret", default=None, metavar="SECRET",
        help="Stellar account secret. Env: DARWIN_STELLAR_SECRET",
    )
    wallet_group.add_argument(
        "--wallet-provider", default="cdp",
        choices=["web3", "cdp", "local-vault"],
        help="Wallet backend (default: cdp). 'cdp' (Coinbase CDP, "
             "non-custodial, per-Darwin TEE-held key — production default); "
             "'local-vault' (per-Darwin keypair minted on spawn and sealed "
             "in the per-Darwin vault — self-custodial for chains CDP "
             "doesn't cover); 'web3' (single shared private key from env — "
             "testnet dev shortcut; refuses mainnet RPC URLs).",
    )
    wallet_group.add_argument(
        "--cdp-api-key-id", default=None, metavar="ID",
        help="Coinbase CDP API key id (when --wallet-provider cdp). "
             "Env: CDP_API_KEY_ID",
    )
    wallet_group.add_argument(
        "--cdp-api-key-secret", default=None, metavar="SECRET",
        help="Coinbase CDP API key secret (when --wallet-provider cdp). "
             "Env: CDP_API_KEY_SECRET. Never log this value.",
    )
    wallet_group.add_argument(
        "--cdp-wallet-secret", default=None, metavar="SECRET",
        help="Coinbase CDP wallet secret for transaction signing. "
             "Env: CDP_WALLET_SECRET",
    )
    wallet_group.add_argument(
        "--network", default="testnet", choices=["testnet", "mainnet"],
        help="Authoritative chain network for the whole platform (CDP wallet, "
             "USDC contract, omnibus, money-safety gate). Default: testnet. "
             "--network mainnet is the single explicit switch to real money.",
    )
    wallet_group.add_argument(
        "--cdp-mainnet", action="store_true",
        help="DEPRECATED alias for --network mainnet (kept for back-compat).",
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
    vault_group.add_argument(
        "--vault-auth", default="token",
        choices=["token", "approle", "kubernetes"],
        help="Vault auth method. 'token' is the dev path (--vault-token); "
             "'approle' and 'kubernetes' are the production methods (leased "
             "tokens, auto-renewed on expiry). Auto-selected when only "
             "AppRole / k8s creds are supplied.",
    )
    vault_group.add_argument(
        "--vault-role-id", default=None, metavar="ID",
        help="AppRole role id (auth_method=approle). Env: DARWIN_VAULT_ROLE_ID",
    )
    vault_group.add_argument(
        "--vault-secret-id", default=None, metavar="SECRET",
        help="AppRole secret id (auth_method=approle). "
             "Env: DARWIN_VAULT_SECRET_ID. Never log this value.",
    )
    vault_group.add_argument(
        "--vault-k8s-role", default=None, metavar="ROLE",
        help="Vault Kubernetes-auth role (auth_method=kubernetes). "
             "Env: DARWIN_VAULT_K8S_ROLE",
    )
    vault_group.add_argument(
        "--vault-k8s-jwt-path", default=None, metavar="PATH",
        help="Service-account JWT path for k8s auth (default: the in-pod "
             "/var/run/secrets/kubernetes.io/serviceaccount/token).",
    )

    browser_group = serve.add_argument_group(
        "real browser (doc 15)",
        "Connect to Browserbase. Requires the [browser] extra. "
        "When omitted the in-memory FakeBrowser is used.",
    )
    browser_group.add_argument(
        "--browser-provider", default="browserbase",
        choices=["browserbase", "browser-use-cloud"],
        help="Real browser substrate (doc 31). 'browserbase' wires the "
             "OSS browser-use library against Browserbase (default). "
             "'browser-use-cloud' wires the V3 Agent API against Browser "
             "Use Cloud's Stealth Browsers (requires [browser-use-cloud] "
             "extra).",
    )
    browser_group.add_argument(
        "--browserbase-url", default=None, metavar="URL",
        help="Browserbase WS endpoint. Env: BROWSERBASE_URL",
    )
    browser_group.add_argument(
        "--browserbase-api-key", default=None, metavar="KEY",
        help="Browserbase API key. Env: BROWSERBASE_API_KEY",
    )
    browser_group.add_argument(
        "--browser-use-api-key", default=None, metavar="KEY",
        help="Browser Use Cloud API key (when "
             "--browser-provider browser-use-cloud). Env: BROWSER_USE_API_KEY",
    )
    browser_group.add_argument(
        "--browser-use-model", default=None, metavar="MODEL",
        help="Optional model name passed to Browser Use Cloud — e.g. a "
             "BU custom model (doc 31). Default: cloud picks.",
    )
    browser_group.add_argument(
        "--browser-use-max-cost-usdc", type=float, default=0.10,
        metavar="USD",
        help="Per-session spend cap for the Browser Use Cloud V3 Agent. "
             "Pre-debited from the wallet and passed to the SDK as "
             "max_cost_usd so cloud billing matches the ledger debit. "
             "Default: 0.10",
    )

    sandbox_group = serve.add_argument_group(
        "real sandbox (doc 14)",
        "Connect to E2B for code-running neurons. Requires the [e2b] extra. "
        "When omitted the in-memory FakeSandbox is used.",
    )
    sandbox_group.add_argument(
        "--e2b-api-key", default=None, metavar="KEY",
        help="E2B API key. Env: E2B_API_KEY",
    )

    compliance_group = serve.add_argument_group(
        "compliance (doc 17)",
        "Sanctions screening run before every wallet outbound transfer. "
        "Default = no screener (development); production wires the OFAC "
        "denylist or a real provider (Chainalysis, TRM Labs) via the "
        "SanctionsScreener Protocol.",
    )
    compliance_group.add_argument(
        "--sanctions-denylist", default=None, metavar="PATH",
        help=(
            "Path to a JSON file: "
            '{"denylist": ["0xdead..."], "denied_prefixes": ["0xff"]}. '
            "Loaded into a LocalDenylistScreener at startup."
        ),
    )
    compliance_group.add_argument(
        "--sanctions-provider", default="local",
        choices=["local", "chainalysis", "trm"],
        help=(
            "Sanctions screening provider. 'local' uses --sanctions-denylist; "
            "'chainalysis' / 'trm' use the real provider's HTTP API "
            "(requires --sanctions-api-key)."
        ),
    )
    compliance_group.add_argument(
        "--sanctions-api-key", default=None, metavar="KEY",
        help="API key for the chosen sanctions provider. "
             "Env: DARWIN_SANCTIONS_API_KEY",
    )

    notify_group = serve.add_argument_group(
        "notification sinks (doc 08)",
        "Where escalation tickets + monitor alerts are sent. Multiple "
        "flags fan out — Slack + webhook + email are all delivered.",
    )
    notify_group.add_argument(
        "--slack-webhook-url", default=None, metavar="URL",
        help="Slack incoming-webhook URL for escalation notifications. "
             "Env: DARWIN_SLACK_WEBHOOK_URL",
    )
    notify_group.add_argument(
        "--escalation-webhook-url", default=None, metavar="URL",
        help="Generic JSON-POST webhook for escalation tickets. "
             "Env: DARWIN_ESCALATION_WEBHOOK_URL",
    )
    notify_group.add_argument(
        "--alert-webhook-url", default=None, metavar="URL",
        help="Generic JSON-POST webhook for monitor alerts. "
             "Env: DARWIN_ALERT_WEBHOOK_URL",
    )
    notify_group.add_argument(
        "--pagerduty-routing-key", default=None, metavar="KEY",
        help="PagerDuty Events v2 routing key for critical monitor "
             "alerts. Env: DARWIN_PAGERDUTY_ROUTING_KEY",
    )

    # -- observability (doc 08 §Observability) ---------------------------
    obs_group = serve.add_argument_group(
        "observability (doc 08)",
        "OpenTelemetry trace export. Without this, spans stay NoOp.",
    )
    obs_group.add_argument(
        "--otlp-endpoint", default=None, metavar="URL",
        help="OTLP/HTTP collector base URL (e.g. http://jaeger:4318). "
             "Spans the supervisor emits are exported here. "
             "Env: OTEL_EXPORTER_OTLP_ENDPOINT",
    )

    # -- real LLM gateway (doc 13) ---------------------------------------
    llm_group = serve.add_argument_group(
        "LLM gateway (doc 13)",
        "Point the genome's LLM transport at a real LiteLLM proxy. "
        "Without this, the deterministic FakeLLM is used.",
    )
    llm_group.add_argument(
        "--litellm-url", default=None, metavar="URL",
        help="LiteLLM proxy base URL (e.g. http://litellm:4000). "
             "Env: DARWIN_LITELLM_URL",
    )
    llm_group.add_argument(
        "--litellm-master-key", default=None, metavar="KEY",
        help="LiteLLM proxy master key (default: dev_only_master_key, "
             "matching docker/litellm.config.yaml). "
             "Env: DARWIN_LITELLM_MASTER_KEY",
    )

    # -- Phase 1: evolution wiring (doc 21) ------------------------------
    evolution_group = serve.add_argument_group(
        "Phase 1 evolution (doc 21)",
        "HSM-backed DNA signing + sandboxed patch staging.",
    )
    evolution_group.add_argument(
        "--signing-vault-transit-key", default=None, metavar="NAME",
        help="Vault Transit key name for HSM-backed DNA signing "
             "(reuses --vault-url / --vault-token). Without this, an "
             "in-process Ed25519 signer is used.",
    )

    # -- Phase 2: multi-tenant services (doc 22) -------------------------
    phase2_group = serve.add_argument_group(
        "Phase 2 services (doc 22)",
        "Optional platform services: drift detection, Merkle anchoring, "
        "omnibus wallet, KYC, FCM push. Each is off unless its flag is "
        "set. The maintenance interval drives their periodic hooks.",
    )
    phase2_group.add_argument(
        "--enable-drift-detection", action="store_true",
        help="Run the behavioural drift detector (rolling 7-window "
             "profile, 2σ alerts).",
    )
    phase2_group.add_argument(
        "--enable-merkle-anchor", action="store_true",
        help="Publish a daily fleet Merkle root for audit tamper-"
             "evidence. Uses an in-memory anchor backend in dev.",
    )
    phase2_group.add_argument(
        "--enable-omnibus", action="store_true",
        help="Run the platform omnibus wallet (requires a real chain "
             "via --rpc-url + --wallet-key).",
    )
    phase2_group.add_argument(
        "--allow-manual-credit", action="store_true",
        help="Enable POST /platform/omnibus/credit, which mints user credit "
             "not tied to a verified deposit. Off by default — a faucet / "
             "testnet affordance. Leave disabled for real-money production.",
    )
    phase2_group.add_argument(
        "--kyc-provider", default="none",
        choices=["none", "stub", "persona", "onfido"],
        help="KYC provider for fund/withdraw gating (default: none).",
    )
    phase2_group.add_argument(
        "--kyc-persona-api-key", default=None, metavar="KEY",
        help="Persona API key (when --kyc-provider persona). "
             "Env: DARWIN_KYC_PERSONA_API_KEY",
    )
    phase2_group.add_argument(
        "--kyc-persona-template-id", default=None, metavar="ID",
        help="Persona inquiry template id (when --kyc-provider persona). "
             "Env: DARWIN_KYC_PERSONA_TEMPLATE_ID",
    )
    phase2_group.add_argument(
        "--kyc-onfido-api-token", default=None, metavar="TOKEN",
        help="Onfido API token (when --kyc-provider onfido). "
             "Env: ONFIDO_API_TOKEN",
    )
    phase2_group.add_argument(
        "--kyc-onfido-workflow-id", default=None, metavar="ID",
        help="Onfido Studio workflow id (when --kyc-provider onfido). "
             "Env: ONFIDO_WORKFLOW_ID",
    )
    phase2_group.add_argument(
        "--kyc-onfido-region", default="eu", choices=["eu", "us", "ca"],
        help="Onfido data region (default: eu).",
    )
    phase2_group.add_argument(
        "--twilio-account-sid", default=None, metavar="SID",
        help="Twilio account SID — enables the SMS 2FA inbox (doc 15). "
             "Env: TWILIO_ACCOUNT_SID",
    )
    phase2_group.add_argument(
        "--twilio-auth-token", default=None, metavar="TOKEN",
        help="Twilio auth token (paired with --twilio-account-sid). "
             "Env: TWILIO_AUTH_TOKEN. Never log this value.",
    )
    phase2_group.add_argument(
        "--twilio-phone-number", default=None, metavar="NUMBER",
        help="Default platform Twilio number that 2FA texts land on. "
             "Env: TWILIO_PHONE_NUMBER",
    )
    phase2_group.add_argument(
        "--fcm-project-id", default=None, metavar="ID",
        help="Firebase project id — enables FCM push for escalation "
             "tickets. Env: DARWIN_FCM_PROJECT_ID",
    )
    phase2_group.add_argument(
        "--push-provider", default="none",
        choices=["none", "webpush"],
        help="Mobile escalation companion push delivery (doc 22). The "
             "PWA path uses W3C Web Push + VAPID; this flag opts in. "
             "Default: none (escalations notify via the in-memory log "
             "sink only).",
    )
    phase2_group.add_argument(
        "--vapid-public-key", default=None, metavar="B64",
        help="Base64url-encoded VAPID public key (P-256). The PWA reads "
             "this from /me/push-vapid-public-key. Env: DARWIN_VAPID_PUBLIC_KEY",
    )
    phase2_group.add_argument(
        "--vapid-private-key", default=None, metavar="PEM_OR_PATH",
        help="VAPID private key — either a PEM-encoded EC private key "
             "string or a file path. Env: DARWIN_VAPID_PRIVATE_KEY",
    )
    phase2_group.add_argument(
        "--vapid-subject", default=None, metavar="URI",
        help="VAPID subject (mailto: URL or https:// site URL) per RFC "
             "8292. Required when --push-provider webpush is set. "
             "Env: DARWIN_VAPID_SUBJECT",
    )
    phase2_group.add_argument(
        "--fiat-provider", default="none",
        choices=["none", "stub", "stripe", "moonpay"],
        help="Fiat on/off-ramp for deposits + withdrawals (doc 17). "
             "Default: none.",
    )
    phase2_group.add_argument(
        "--stripe-api-key", default=None, metavar="KEY",
        help="Stripe secret key (when --fiat-provider stripe). "
             "Env: STRIPE_API_KEY",
    )
    phase2_group.add_argument(
        "--stripe-webhook-secret", default=None, metavar="SECRET",
        help="Stripe webhook signing secret; enables POST /webhooks/stripe "
             "to verify + auto-confirm deposits. Env: STRIPE_WEBHOOK_SECRET",
    )
    phase2_group.add_argument(
        "--moonpay-api-key", default=None, metavar="KEY",
        help="MoonPay API key (when --fiat-provider moonpay). "
             "Env: MOONPAY_API_KEY",
    )
    phase2_group.add_argument(
        "--episodic-memory", default="deterministic",
        choices=["deterministic", "lancedb"],
        help="Per-Darwin episodic memory substrate (doc 03). "
             "'deterministic' keeps the in-process EpisodicMemory the "
             "genome ships with (sim + cross-engine parity). 'lancedb' "
             "installs a LanceDbEpisodicMemory per Darwin under "
             "--lancedb-path with embeddings via the LiteLLM "
             "tier-embedding model. Default: deterministic.",
    )
    phase2_group.add_argument(
        "--lancedb-path", default=None, metavar="DIR",
        help="Base directory for per-Darwin LanceDB tables (one subdir "
             "per darwin_id) when --episodic-memory lancedb. "
             "Env: DARWIN_LANCEDB_PATH",
    )
    phase2_group.add_argument(
        "--lancedb-embedding-model", default=None, metavar="MODEL",
        help="LiteLLM model for episodic-memory embeddings (when "
             "--episodic-memory lancedb). "
             "Default: openai/text-embedding-3-small.",
    )
    phase2_group.add_argument(
        "--maintenance-interval", default=86400.0, type=float,
        metavar="SECONDS",
        help="Wall-clock gap between drive-loop maintenance runs "
             "(drift profile, Merkle anchor, omnibus reconciliation). "
             "Default 86400 (daily); set low for a soak.",
    )

    k8s_group = serve.add_argument_group(
        "K8s driver (doc 08 production posture)",
        "Per-Darwin namespace + orchestrator microVM. Requires the "
        "[k8s] extra. With --driver=in-process (default) every Darwin "
        "is an asyncio.Task in this supervisor pod; with --driver=k8s "
        "the supervisor provisions per-Darwin namespaces and drives "
        "lifecycle over HTTP RPC to the orchestrator pods.",
    )
    k8s_group.add_argument(
        "--driver", default="in-process", choices=["in-process", "k8s"],
        help="Per-Darwin runtime location. Default: in-process.",
    )
    k8s_group.add_argument(
        "--k8s-namespace-prefix", default="darwin-",
        help="Per-Darwin namespace prefix (default: 'darwin-').",
    )
    k8s_group.add_argument(
        "--k8s-runtime-class", default="kata",
        help="RuntimeClass for the orchestrator pod (default: 'kata').",
    )
    k8s_group.add_argument(
        "--k8s-orchestrator-image", default=None,
        help="Image:tag for the orchestrator pod. Default reuses the "
        "supervisor image (the entrypoint differs).",
    )
    k8s_group.add_argument(
        "--k8s-egress-proxy-namespace", default="platform-data",
        help="Namespace hosting the egress proxy "
             "(NetworkPolicy peer for per-Darwin orchestrator pods).",
    )
    k8s_group.add_argument(
        "--k8s-lancedb-image", default="lancedb/lancedb:latest",
        help="Image for the per-Darwin LanceDB sidecar.",
    )
    k8s_group.add_argument(
        "--k8s-lancedb-storage-size", default="1Gi",
        help="PVC size for the LanceDB sidecar (default: 1Gi).",
    )
    k8s_group.add_argument(
        "--k8s-lancedb-storage-class", default=None,
        help="StorageClass for the LanceDB PVC. Empty → cluster default.",
    )
    k8s_group.add_argument(
        "--k8s-postgres-namespace", default=None,
        help="Postgres namespace, for NetworkPolicy egress from "
             "orchestrator pods. Empty omits the rule (SQLite mode).",
    )
    k8s_group.add_argument(
        "--k8s-postgres-pod-label", default=None, metavar="KEY=VAL",
        help="Postgres pod-label selector (e.g. "
             "'app.kubernetes.io/name=postgres').",
    )
    k8s_group.add_argument(
        "--k8s-postgres-port", default=5432, type=int,
        help="Postgres port (default: 5432).",
    )
    k8s_group.add_argument(
        "--k8s-database-url-secret", default=None, metavar="NAME[:KEY]",
        help="Secret reference for the orchestrator's DATABASE_URL env. "
             "Format: 'secret-name' or 'secret-name:key' "
             "(default key: DATABASE_URL).",
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
