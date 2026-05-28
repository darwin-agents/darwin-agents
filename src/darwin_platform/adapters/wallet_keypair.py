"""Per-Darwin wallet keypair minting for the ``local-vault`` provider.

When the operator selects ``--wallet-provider local-vault``, the wallet
factory mints a fresh keypair the first time a Darwin spawns and seals
the secret in that Darwin's per-Darwin vault namespace. Subsequent
spawns (resume after restart) read the sealed secret and rebuild the
chain client against the same address — the wallet survives a process
bounce without leaking the key into the supervisor's environment.

Layout:

- :class:`WalletKeypair` — frozen result of one mint (chain + secret +
  on-chain address).
- :class:`WalletKeypairMinter` — Protocol for generating a fresh keypair
  for one chain.
- :class:`SolanaKeypairMinter`, :class:`StellarKeypairMinter`,
  :class:`EvmKeypairMinter` — concrete impls gated on their respective
  optional SDK extras (slim installs raise a clear hint).
- :func:`ensure_keypair` — read-or-mint helper the factory calls. Pure
  glue around the vault's ``get``/``put`` so it's testable without any
  chain SDK or real vault.

The secret lives at ``sensitive/wallet/{chain}/secret`` and the address
at ``sensitive/wallet/{chain}/address`` inside the per-Darwin vault. The
``sensitive`` tier triggers the platform's KYC-style retention sweep
(doc 16); for a wallet that's used every heartbeat the TTL effectively
never expires (each read refreshes it via :class:`VaultRetentionService`
when the platform-side admin service is wired).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


SUPPORTED_CHAINS = ("solana", "stellar", "base")


@dataclass(frozen=True)
class WalletKeypair:
    """One minted keypair. ``secret`` is whatever the chain SDK expects
    on instantiation (hex for EVM/Solana, strkey for Stellar); the
    address is what gets handed to counter-parties."""

    chain: str
    secret: str
    address: str


@runtime_checkable
class WalletKeypairMinter(Protocol):
    """Generates a single fresh keypair. No persistence — callers seal
    the result in the vault."""

    chain: str

    def mint(self) -> WalletKeypair: ...


class SolanaKeypairMinter:
    """Mints an ed25519 keypair via ``solders``. Requires the [solana]
    extra (``solana`` + ``solders``)."""

    chain = "solana"

    def mint(self) -> WalletKeypair:
        try:
            from solders.keypair import Keypair  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "local-vault solana minter requires the [solana] extra: "
                "pip install 'darwin-dna[solana]'"
            ) from exc
        kp = Keypair()
        return WalletKeypair(
            chain="solana",
            secret=bytes(kp).hex(),
            address=str(kp.pubkey()),
        )


class StellarKeypairMinter:
    """Mints a fresh Stellar keypair. Requires the [stellar] extra."""

    chain = "stellar"

    def mint(self) -> WalletKeypair:
        try:
            from stellar_sdk import Keypair  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "local-vault stellar minter requires the [stellar] extra: "
                "pip install 'darwin-dna[stellar]'"
            ) from exc
        kp = Keypair.random()
        return WalletKeypair(
            chain="stellar",
            secret=kp.secret,
            address=kp.public_key,
        )


class EvmKeypairMinter:
    """Mints a fresh secp256k1 keypair for Base / EVM. Requires the
    [web3] extra (``eth_account`` ships with ``web3``)."""

    chain = "base"

    def mint(self) -> WalletKeypair:
        try:
            from eth_account import Account  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "local-vault evm minter requires the [web3] extra: "
                "pip install 'darwin-dna[web3]'"
            ) from exc
        acct = Account.create()
        # eth_account returns a HexBytes for .key; .hex() yields "0x…"
        # which Web3ChainClient accepts directly.
        return WalletKeypair(
            chain="base",
            secret=acct.key.hex(),
            address=acct.address,
        )


def secret_path(chain: str) -> str:
    """Vault key under which the chain's secret bytes live."""
    return f"sensitive/wallet/{chain}/secret"


def address_path(chain: str) -> str:
    """Vault key under which the chain's on-chain address lives. Kept
    alongside the secret so the factory can return both without
    re-deriving (some chains' derivation is non-trivial / async)."""
    return f"sensitive/wallet/{chain}/address"


def ensure_keypair(
    vault,
    *,
    chain: str,
    minter: WalletKeypairMinter,
) -> WalletKeypair:
    """Return the per-Darwin keypair for ``chain``: read from the vault
    if sealed, otherwise mint via ``minter``, seal, and return.

    Pure glue — ``vault`` only needs ``get(key) -> str | None`` and
    ``put(key, value)``. Both the genome's :class:`TieredVault` and the
    test :class:`FakeVault` satisfy that. The factory wires this against
    the per-Darwin vault that the Supervisor already installed before
    calling the wallet factory."""
    if chain not in SUPPORTED_CHAINS:
        raise ValueError(
            f"unknown chain {chain!r}; "
            f"expected one of {SUPPORTED_CHAINS}"
        )
    if minter.chain != chain:
        raise ValueError(
            f"minter is for chain {minter.chain!r}, expected {chain!r}"
        )
    sk = secret_path(chain)
    ak = address_path(chain)
    existing = vault.get(sk)
    if existing:
        return WalletKeypair(
            chain=chain,
            secret=existing,
            address=vault.get(ak) or "",
        )
    kp = minter.mint()
    vault.put(sk, kp.secret)
    vault.put(ak, kp.address)
    return kp
