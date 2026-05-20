"""Multi-chain wallet support (docs/engineering/17 §"Fallback chains").

Doc 17 ramp model is USDC-on-Base for v1. Doc 17 also commits to
Solana (higher TPS) and Stellar (cheap micropayments) as fallback
networks. This module ships the platform-side machinery so a Darwin's
wallet can route a transfer across the cheapest viable network for the
recipient.

Architecture:

- :class:`ChainNetwork` enum — known chains.
- :class:`SolanaChainClient` — SPL USDC against Solana RPC via the
  injected ``solana-py`` client.
- :class:`StellarChainClient` — USDC issuance on Stellar via the
  injected ``stellar-sdk`` client.
- :class:`MultiChainRouter` — picks a chain per (recipient_address,
  amount_usdc, policy). Falls back through the configured chain
  preference list until one accepts.

Each chain client conforms to the existing
:class:`darwin_platform.adapters.wallet.ChainClient` Protocol so it
plugs into :class:`RealWallet` unchanged. The router presents the
same surface but multiplexes across multiple clients.

The SDK objects are user-injected (test stubs in unit tests; real
``solana.rpc.api.Client`` + ``stellar_sdk.Server`` in production).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable


class ChainNetwork(str, Enum):
    BASE = "base"
    SOLANA = "solana"
    STELLAR = "stellar"


class ChainRoutingError(Exception):
    """The router couldn't find a chain that would accept the transfer."""


# ---------------------------------------------------------------------------
# Solana — SPL USDC
# ---------------------------------------------------------------------------

# USDC SPL mint on Solana mainnet.
USDC_MINT_SOLANA_MAINNET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_MINT_SOLANA_DEVNET = "Gh9ZwEmdLJ8DscKNTkTqPbNwLNNBjuSzaG9Vp2KGtKJr"


class SolanaChainClient:
    """ChainClient over Solana. Holds a Solana RPC client and a
    Keypair (or signer object compatible with solana-py); never logs
    the secret key. USDC transfers go through the SPL Token program
    via the associated token account model."""

    def __init__(
        self,
        *,
        rpc_client: Any,
        keypair: Any,
        usdc_mint: str = USDC_MINT_SOLANA_MAINNET,
    ) -> None:
        if rpc_client is None or keypair is None:
            raise ValueError(
                "SolanaChainClient requires rpc_client + keypair"
            )
        self._rpc = rpc_client
        self._kp = keypair
        self._mint = usdc_mint

    def address(self) -> str:
        # keypair.public_key (older solana-py) or .pubkey() (newer).
        pk = getattr(self._kp, "public_key", None)
        if pk is None:
            pk = getattr(self._kp, "pubkey", lambda: None)()
        if pk is None:
            raise RuntimeError("keypair exposes no public key")
        return str(pk)

    def balance_usdc(self) -> float:
        """Read the USDC SPL balance. Returns 0.0 if the associated
        token account doesn't exist yet (Darwin hasn't been funded)."""
        try:
            resp = self._rpc.get_token_accounts_by_owner(
                self.address(), mint=self._mint,
            )
        except Exception as exc:
            raise RuntimeError(f"solana balance read failed: {exc}") from exc
        # solana-py returns {"value": [{"pubkey": ..., "account": ...}]}
        value = (
            resp.get("value") if isinstance(resp, dict)
            else getattr(resp, "value", [])
        )
        if not value:
            return 0.0
        first = value[0]
        info = (
            first.get("account", {}).get("data", {}).get("parsed", {})
            .get("info", {}).get("tokenAmount", {})
            if isinstance(first, dict) else {}
        )
        ui_amount = info.get("uiAmount", 0.0) if isinstance(info, dict) else 0.0
        return float(ui_amount or 0.0)

    def transfer(
        self, *, to: str, amount_usdc: float, memo: str,
    ) -> dict:
        """Submit an SPL token transfer. Real impl uses
        ``spl.token.instructions.transfer_checked``; here we delegate
        to whatever the user-supplied ``rpc_client`` exposes as
        ``transfer_usdc(to, amount, memo)`` — production wires this
        properly, tests inject a stub."""
        if amount_usdc <= 0:
            raise ValueError("amount_usdc must be > 0")
        sender = getattr(self._rpc, "transfer_usdc", None)
        if not callable(sender):
            raise RuntimeError(
                "rpc_client must expose transfer_usdc(to, amount, memo) — "
                "production deployers wire this to spl.token.instructions"
            )
        result = sender(to=to, amount=amount_usdc, memo=memo)
        if isinstance(result, dict):
            return result
        return {"tx_hash": str(result)}


# ---------------------------------------------------------------------------
# Stellar — USDC issuance
# ---------------------------------------------------------------------------

STELLAR_USDC_ISSUER_MAINNET = (
    "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"  # Circle
)


class StellarChainClient:
    """ChainClient over Stellar. The injected ``stellar_server`` is
    a ``stellar_sdk.Server`` (or compatible stub); ``keypair`` is a
    ``stellar_sdk.Keypair``.

    USDC on Stellar is the Centre/Circle-issued asset; the issuer
    account is fixed (``STELLAR_USDC_ISSUER_MAINNET``)."""

    def __init__(
        self,
        *,
        stellar_server: Any,
        keypair: Any,
        usdc_issuer: str = STELLAR_USDC_ISSUER_MAINNET,
        network_passphrase: str | None = None,
    ) -> None:
        if stellar_server is None or keypair is None:
            raise ValueError(
                "StellarChainClient requires stellar_server + keypair"
            )
        self._server = stellar_server
        self._kp = keypair
        self._issuer = usdc_issuer
        self._network = network_passphrase

    def address(self) -> str:
        pk = getattr(self._kp, "public_key", None)
        if pk is None:
            getter = getattr(self._kp, "public_key", None) or getattr(
                self._kp, "address", lambda: None,
            )
            pk = getter() if callable(getter) else getter
        if pk is None:
            raise RuntimeError("keypair exposes no public key")
        return str(pk)

    def balance_usdc(self) -> float:
        try:
            account = self._server.accounts().account_id(self.address()).call()
        except Exception as exc:
            raise RuntimeError(f"stellar balance read failed: {exc}") from exc
        # Stellar returns balances as a list of {asset_code, balance, ...}.
        balances = (
            account.get("balances", []) if isinstance(account, dict)
            else getattr(account, "balances", [])
        )
        for b in balances:
            asset_code = (
                b.get("asset_code") if isinstance(b, dict)
                else getattr(b, "asset_code", None)
            )
            issuer = (
                b.get("asset_issuer") if isinstance(b, dict)
                else getattr(b, "asset_issuer", None)
            )
            if asset_code == "USDC" and issuer == self._issuer:
                amount = (
                    b.get("balance") if isinstance(b, dict)
                    else getattr(b, "balance", "0")
                )
                return float(amount)
        return 0.0

    def transfer(
        self, *, to: str, amount_usdc: float, memo: str,
    ) -> dict:
        if amount_usdc <= 0:
            raise ValueError("amount_usdc must be > 0")
        sender = getattr(self._server, "transfer_usdc", None)
        if not callable(sender):
            raise RuntimeError(
                "stellar_server must expose transfer_usdc(to, amount, "
                "memo, issuer) — production wires this to stellar_sdk's "
                "TransactionBuilder.append_payment_op"
            )
        result = sender(
            to=to, amount=amount_usdc, memo=memo, issuer=self._issuer,
        )
        if isinstance(result, dict):
            return result
        return {"tx_hash": str(result)}


# ---------------------------------------------------------------------------
# MultiChainRouter — picks a chain per transfer
# ---------------------------------------------------------------------------

# Heuristic detection: which chain does this address belong to?
def detect_network(address: str) -> ChainNetwork | None:
    """Cheap address-shape heuristic. Production deployers may pass a
    custom resolver into the router for richer detection (e.g. ENS,
    name-service lookups)."""
    addr = address.strip()
    if not addr:
        return None
    # EVM addresses: 0x + 40 hex chars.
    if addr.startswith("0x") and len(addr) == 42:
        try:
            int(addr[2:], 16)
            return ChainNetwork.BASE
        except ValueError:
            return None
    # Stellar: starts with G, 56 chars total.
    if addr.startswith("G") and len(addr) == 56:
        return ChainNetwork.STELLAR
    # Solana addresses are base58, typically 32-44 chars. The cheap
    # heuristic: not 0x-prefixed, not G-prefixed-56, looks base58.
    if 32 <= len(addr) <= 44 and not addr.startswith(("0x", "G")):
        # Looks Solana-shaped.
        return ChainNetwork.SOLANA
    return None


@runtime_checkable
class NetworkDetector(Protocol):
    def __call__(self, address: str) -> ChainNetwork | None: ...


class MultiChainRouter:
    """Routes outbound USDC transfers across multiple chain clients.

    Construction takes a mapping of ``ChainNetwork → ChainClient`` and
    a preferred-order list. ``transfer`` tries the chain matching the
    recipient address (via ``detect_network`` or a user-supplied
    resolver); if that fails OR no match, falls through the preferred
    order until one client accepts.

    The router itself satisfies the same ``ChainClient`` Protocol so
    :class:`RealWallet` can use it directly."""

    def __init__(
        self,
        *,
        clients: dict[ChainNetwork, Any],
        preferred: list[ChainNetwork] | None = None,
        detector: Callable[[str], ChainNetwork | None] | None = None,
    ) -> None:
        if not clients:
            raise ValueError("MultiChainRouter needs at least one client")
        self._clients: dict[ChainNetwork, Any] = dict(clients)
        self._preferred = list(preferred or list(self._clients))
        self._detector = detector or detect_network

    def address(self) -> str:
        # The router's "own" address is the primary chain's — that's
        # the Darwin's canonical incoming address. Other chains'
        # addresses are reachable via .clients[net].address().
        primary = self._preferred[0] if self._preferred else next(iter(self._clients))
        return self._clients[primary].address()

    def balance_usdc(self) -> float:
        """Sum of USDC across all chains. RealWallet uses this for
        the authoritative balance check; spending across chains works
        as long as the total covers the amount and at least one chain
        has enough on its own."""
        total = 0.0
        for client in self._clients.values():
            try:
                total += float(client.balance_usdc())
            except Exception:
                # Skip a chain whose RPC is momentarily unreachable;
                # the transfer attempt below will surface the issue.
                continue
        return total

    def transfer(
        self,
        *,
        to: str,
        amount_usdc: float,
        memo: str,
    ) -> dict:
        order: list[ChainNetwork] = []
        # 1. Address-matched chain first.
        detected = self._detector(to)
        if detected is not None and detected in self._clients:
            order.append(detected)
        # 2. Then the preferred order, skipping any we already queued.
        for net in self._preferred:
            if net in self._clients and net not in order:
                order.append(net)
        last_exc: Exception | None = None
        for net in order:
            client = self._clients[net]
            try:
                # Skip if the chain doesn't have enough USDC on its own —
                # cross-chain bridging is OUT of scope for v1; the operator
                # rebalances liquidity off-band.
                if client.balance_usdc() + 1e-9 < amount_usdc:
                    continue
                result = client.transfer(
                    to=to, amount_usdc=amount_usdc, memo=memo,
                )
                if isinstance(result, dict):
                    result.setdefault("chain", net.value)
                    return result
                return {"tx_hash": str(result), "chain": net.value}
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise ChainRoutingError(
                f"no chain accepted {to!r} for ${amount_usdc:.2f} USDC: "
                f"last error: {last_exc!r}"
            )
        raise ChainRoutingError(
            f"no chain has enough liquidity for ${amount_usdc:.2f} USDC to {to!r}"
        )

    @property
    def clients(self) -> dict[ChainNetwork, Any]:
        return dict(self._clients)
