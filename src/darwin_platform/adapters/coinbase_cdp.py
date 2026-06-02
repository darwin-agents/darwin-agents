"""Coinbase CDP (Agentic Wallet) chain adapter — docs/engineering/05, 17.

A ``ChainClient`` whose private key never enters this process: the
per-Darwin wallet is a Coinbase Developer Platform account, signed inside
Coinbase's TEE. This is the non-custodial alternative to the raw-private-
key :class:`~darwin_platform.adapters.web3_chain.Web3ChainClient`.

Layering mirrors the Solana / Stellar adapters:

- :class:`CdpChainClient` — implements the genome's ``ChainClient``
  contract (``address`` / ``balance_usdc`` / ``transfer``) over an
  injected :class:`CdpAccount`. Pure and fully unit-testable with a stub
  account — no SDK, no network.
- :func:`build_cdp_chain_client` — constructs the real thing from CDP API
  credentials: opens a ``cdp-sdk`` client, gets-or-creates the EVM
  account, wraps it in :class:`_RealCdpAccount`.

The CDP SDK is async; ``ChainClient`` is sync. :class:`_AsyncBridge`
drives the coroutines on a dedicated background event loop, so a sync
transfer works even when called from inside the supervisor's own loop.

``cdp-sdk``'s account/transfer surface is version-sensitive — every
SDK-touching call is isolated in :class:`_RealCdpAccount`. If a future
SDK release moves a method, that one class is the only thing to update;
:class:`CdpChainClient` and the genome contract stay put.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable


class CdpSpendPolicyError(Exception):
    """A transfer was refused by the wallet-level spend policy."""


@dataclass
class CdpSpendPolicy:
    """An independent spend cap enforced at the CDP wallet boundary (doc 24).

    Defense-in-depth BELOW the in-process treasury policy: even if that policy
    is bypassed or buggy, the wallet adapter itself refuses an over-cap or
    off-allowlist transfer, so a platform bug cannot drain the real CDP
    account. (Coinbase's server-side spend permissions are the ideal home for
    this; this adapter-level enforcement is the portable equivalent.)"""

    per_tx_usdc: float | None = None
    per_day_usdc: float | None = None
    allowlist: frozenset[str] = frozenset()
    clock: Callable[[], float] = time.time
    _spent_today: float = field(default=0.0, init=False, repr=False)
    _day: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        self.allowlist = frozenset(
            str(a).strip().lower() for a in self.allowlist if str(a).strip()
        )

    def _roll(self) -> None:
        d = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        if d != self._day:
            self._day = d
            self._spent_today = 0.0

    def authorize(self, *, to: str, amount_usdc: float) -> None:
        """Raise :class:`CdpSpendPolicyError` if the transfer violates policy."""
        self._roll()
        if self.allowlist and str(to).strip().lower() not in self.allowlist:
            raise CdpSpendPolicyError(
                f"recipient {to!r} not on the CDP wallet allowlist"
            )
        if self.per_tx_usdc is not None and amount_usdc > self.per_tx_usdc:
            raise CdpSpendPolicyError(
                f"amount ${amount_usdc:.2f} exceeds CDP per-tx cap "
                f"${self.per_tx_usdc:.2f}"
            )
        if (
            self.per_day_usdc is not None
            and (self._spent_today + amount_usdc) > self.per_day_usdc
        ):
            raise CdpSpendPolicyError(
                f"amount ${amount_usdc:.2f} would exceed CDP daily cap "
                f"${self.per_day_usdc:.2f} (spent ${self._spent_today:.2f})"
            )

    def record(self, amount_usdc: float) -> None:
        self._roll()
        self._spent_today += float(amount_usdc)


# CDP requires account names to match this exactly: 2-36 chars, alphanumeric
# at both ends, only ``[A-Za-z0-9-]`` between. ``darwin-<uuid>`` (43 chars)
# overflows it, so callers' names are coerced through ``_coerce_cdp_account_name``.
_CDP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,34}[A-Za-z0-9]$")


def _coerce_cdp_account_name(name: str) -> str:
    """Map any caller-supplied name to a CDP-valid account name.

    Idempotent: a name already in-spec is returned unchanged, so existing
    on-chain accounts keep resolving via ``get_or_create_account``. An
    overflowing name (e.g. ``darwin-<uuid>``) is stripped to its
    alphanumerics; if still too long, a deterministic 12-char blake2 digest
    of the *original* name is appended so distinct Darwins never collide
    while the same Darwin always maps to the same account on resume.
    """
    if _CDP_NAME_RE.match(name):
        return name
    cleaned = re.sub(r"[^A-Za-z0-9]", "", name)
    if 2 <= len(cleaned) <= 36:
        return cleaned
    digest = hashlib.blake2b(name.encode(), digest_size=6).hexdigest()  # 12 hex
    prefix = cleaned[: 36 - 1 - len(digest)]
    return f"{prefix}-{digest}" if prefix else digest


@runtime_checkable
class CdpAccount(Protocol):
    """The minimal sync surface :class:`CdpChainClient` needs from a CDP
    account. :class:`_RealCdpAccount` adapts the live SDK to this; tests
    inject a stub."""

    def address(self) -> str: ...

    def balance_usdc(self) -> float: ...

    def transfer_usdc(
        self, *, to: str, amount_usdc: float, memo: str
    ) -> Any: ...


class CdpChainClient:
    """``ChainClient`` backed by a Coinbase CDP account. The key lives in
    Coinbase's TEE — this process only holds API credentials."""

    name = "coinbase-cdp"

    def __init__(
        self,
        *,
        account: CdpAccount,
        network: str = "base-sepolia",
        spend_policy: CdpSpendPolicy | None = None,
    ):
        if account is None:
            raise ValueError("CdpChainClient requires a CdpAccount")
        self._account = account
        self._network = network
        self._spend_policy = spend_policy

    def address(self) -> str:
        return str(self._account.address())

    def balance_usdc(self) -> float:
        return float(self._account.balance_usdc())

    def transfer(self, *, to: str, amount_usdc: float, memo: str) -> dict:
        amount = float(amount_usdc)
        if amount <= 0:
            raise ValueError("amount_usdc must be > 0")
        if not to:
            raise ValueError("transfer requires a destination address")
        # Independent wallet-level cap (doc 24): refuse BEFORE the CDP call so
        # a platform/treasury-policy bug cannot drain the account.
        if self._spend_policy is not None:
            self._spend_policy.authorize(to=to, amount_usdc=amount)
        result = self._account.transfer_usdc(
            to=to, amount_usdc=amount, memo=memo
        )
        tx_hash = _extract_tx_hash(result)
        if not tx_hash:
            raise RuntimeError("CDP transfer returned no transaction hash")
        if self._spend_policy is not None:
            self._spend_policy.record(amount)
        return {
            "tx_hash": tx_hash,
            "memo": memo,
            "network": self._network,
            "provider": self.name,
        }


def _extract_tx_hash(result: Any) -> str:
    """Pull a tx hash out of whatever the account.transfer call returned —
    a dict, an object with a hash attribute, or a bare string."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("tx_hash", "transaction_hash", "transactionHash", "hash"):
            if result.get(key):
                return str(result[key])
        return ""
    for attr in ("tx_hash", "transaction_hash", "transactionHash", "hash"):
        val = getattr(result, attr, None)
        if val:
            return str(val)
    return ""


# -- async → sync bridge ---------------------------------------------------

class _AsyncBridge:
    """Runs coroutines to completion on a dedicated background event loop.

    The CDP SDK is async; the genome calls the wallet synchronously, often
    from *inside* the supervisor's running loop — where ``asyncio.run``
    would raise. Submitting to a separate loop on its own daemon thread
    sidesteps that entirely."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True,
            name="cdp-async-bridge",
        )
        self._thread.start()

    def run(self, coro: Any) -> Any:
        """Block until ``coro`` completes on the background loop; return
        its result (or re-raise its exception)."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


# -- real SDK glue (version-sensitive — the only thing to touch on an SDK
#    upgrade) --------------------------------------------------------------

class _RealCdpAccount:
    """Adapts a live ``cdp-sdk`` EVM account to the :class:`CdpAccount`
    surface. Every coroutine is driven through the :class:`_AsyncBridge`.

    Targets ``cdp-sdk`` v1.x. If a future release renames a method,
    this class is the single place to adjust."""

    _USDC_DECIMALS = 6

    def __init__(
        self, *, client: Any, account: Any, network: str,
        bridge: _AsyncBridge,
    ) -> None:
        self._client = client
        self._account = account
        self._network = network
        self._bridge = bridge

    def address(self) -> str:
        addr = getattr(self._account, "address", None)
        if addr is None and isinstance(self._account, dict):
            addr = self._account.get("address")
        return str(addr or "")

    def balance_usdc(self) -> float:
        balances = self._bridge.run(
            self._client.evm.list_token_balances(
                address=self.address(), network=self._network,
            )
        )
        rows = getattr(balances, "balances", None)
        if rows is None and isinstance(balances, dict):
            rows = balances.get("balances", [])
        for row in rows or []:
            token = getattr(row, "token", None) or (
                row.get("token") if isinstance(row, dict) else None
            )
            symbol = (
                getattr(token, "symbol", "") or (
                    token.get("symbol", "") if isinstance(token, dict) else ""
                )
            )
            if str(symbol).upper() != "USDC":
                continue
            amount = getattr(row, "amount", None) or (
                row.get("amount") if isinstance(row, dict) else None
            )
            return _atomic_to_usdc(amount, self._USDC_DECIMALS)
        return 0.0

    def transfer_usdc(
        self, *, to: str, amount_usdc: float, memo: str
    ) -> Any:
        return self._bridge.run(
            self._account.transfer(
                to=to, amount=amount_usdc, token="usdc",
                network=self._network,
            )
        )


def _atomic_to_usdc(amount: Any, decimals: int) -> float:
    """Best-effort: CDP may report a token balance as whole units or as an
    atomic-unit string with a separate decimals field."""
    if amount is None:
        return 0.0
    # An object with .amount / .decimals (the v1 TokenAmount shape).
    inner = getattr(amount, "amount", None)
    if inner is not None:
        dec = int(getattr(amount, "decimals", decimals) or decimals)
        return float(inner) / (10 ** dec)
    try:
        val = float(amount)
    except (TypeError, ValueError):
        return 0.0
    # A large integer is almost certainly atomic units.
    return val / (10 ** decimals) if val >= 10 ** decimals else val


def build_cdp_chain_client(
    *,
    api_key_id: str,
    api_key_secret: str,
    wallet_secret: str | None = None,
    network: str = "base-sepolia",
    account_name: str = "darwin-wallet",
    spend_policy: CdpSpendPolicy | None = None,
) -> CdpChainClient:
    """Construct a CDP-backed :class:`CdpChainClient` from API credentials.

    Opens a ``cdp-sdk`` client, gets-or-creates the named EVM account, and
    wraps it. ``network`` is ``base-sepolia`` (default) or ``base-mainnet``.
    """
    if not api_key_id or not api_key_secret:
        raise ValueError("CDP requires api_key_id + api_key_secret")
    try:
        from cdp import CdpClient  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - operator hint
        raise ImportError(
            "Coinbase CDP wallet requires the [cdp] extra: "
            "pip install 'darwin-dna[cdp]'"
        ) from exc

    bridge = _AsyncBridge()
    client = CdpClient(
        api_key_id=api_key_id,
        api_key_secret=api_key_secret,
        wallet_secret=wallet_secret,
    )
    account = bridge.run(
        client.evm.get_or_create_account(
            name=_coerce_cdp_account_name(account_name)
        )
    )
    real = _RealCdpAccount(
        client=client, account=account, network=network, bridge=bridge,
    )
    return CdpChainClient(
        account=real, network=network, spend_policy=spend_policy,
    )
