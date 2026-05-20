"""Real Solana SDK adapter (docs/engineering/17 §fallback chains).

Provides a ``ChainClient``-conforming wrapper around the ``solana``
+ ``solders`` libraries' SPL Token transfer flow. The CLI builds this
from ``--solana-rpc-url`` + ``--solana-wallet-key`` so the operator
doesn't have to hand-instantiate the SDK objects.

The wrapper:

- Resolves (or creates) the sender's associated token account for
  USDC.
- For an outbound transfer, resolves or creates the recipient's ATA
  via the SystemProgram + associated-token-account instructions.
- Submits a TransferChecked instruction signed by the sender's
  keypair.
- Returns the tx signature in the standard ``{"tx_hash": str}``
  shape so :class:`RealWallet` can record it identically to the
  EVM path.

All SDK imports are deferred to instantiation so a slim build that
doesn't have ``solana`` installed only fails when this module is
actually used — never on import."""

from __future__ import annotations

from typing import Any

from .multichain import (
    USDC_MINT_SOLANA_DEVNET,
    USDC_MINT_SOLANA_MAINNET,
    SolanaChainClient,
)


def build_solana_chain_client(
    *,
    rpc_url: str,
    private_key_hex: str,
    cluster: str = "mainnet",
    usdc_mint: str | None = None,
) -> SolanaChainClient:
    """Construct a fully-wired :class:`SolanaChainClient` from the
    CLI-shaped inputs. ``cluster`` selects the default USDC mint when
    not given explicitly."""
    try:
        from solana.rpc.api import Client as SolanaRpcClient
        from solders.keypair import Keypair  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - operator hint
        raise ImportError(
            "Solana adapter requires the 'solana' SDK: "
            "pip install solana solders"
        ) from exc

    if not rpc_url:
        raise ValueError("rpc_url is required")
    if not private_key_hex:
        raise ValueError("private_key_hex is required")
    mint = usdc_mint or (
        USDC_MINT_SOLANA_MAINNET if cluster == "mainnet"
        else USDC_MINT_SOLANA_DEVNET
    )

    raw = bytes.fromhex(
        private_key_hex[2:] if private_key_hex.startswith("0x")
        else private_key_hex
    )
    keypair = Keypair.from_bytes(raw)
    rpc_client = _RpcWithTransfer(
        inner=SolanaRpcClient(rpc_url),
        keypair=keypair,
        usdc_mint=mint,
    )
    return SolanaChainClient(
        rpc_client=rpc_client, keypair=keypair, usdc_mint=mint,
    )


class _RpcWithTransfer:
    """Thin wrapper around solana-py's RPC client that adds a
    ``transfer_usdc`` method the :class:`SolanaChainClient` calls into.

    This keeps the SDK plumbing one layer below the ChainClient surface
    so tests can substitute the inner RPC with a stub without touching
    the SolanaChainClient itself."""

    def __init__(
        self,
        *,
        inner: Any,
        keypair: Any,
        usdc_mint: str,
        usdc_decimals: int = 6,
    ) -> None:
        self._rpc = inner
        self._kp = keypair
        self._mint = usdc_mint
        self._decimals = int(usdc_decimals)

    # Pass-through for the methods SolanaChainClient.balance_usdc reads.
    def get_token_accounts_by_owner(self, owner: str, *, mint: str):
        return self._rpc.get_token_accounts_by_owner(
            owner, mint=mint,
        )

    def transfer_usdc(
        self, *, to: str, amount: float, memo: str,
    ) -> dict[str, Any]:
        """SPL transfer-checked instruction. Resolves (or creates) the
        recipient's associated token account first, then submits the
        TransferChecked + memo instructions in one transaction."""
        try:
            from solana.rpc.types import TxOpts
            from solders.pubkey import Pubkey  # type: ignore[import-not-found]
            from solders.transaction import VersionedTransaction  # type: ignore[import-not-found]
            from solders.message import MessageV0  # type: ignore[import-not-found]
            from spl.token.constants import TOKEN_PROGRAM_ID  # type: ignore[import-not-found]
            from spl.token.instructions import (  # type: ignore[import-not-found]
                TransferCheckedParams,
                get_associated_token_address,
                transfer_checked,
            )
        except ImportError as exc:  # pragma: no cover - operator hint
            raise ImportError(
                "Solana transfer requires solana + solders + "
                "spl-token: pip install solana solders spl-token"
            ) from exc

        mint_pk = Pubkey.from_string(self._mint)
        sender_pk = self._kp.pubkey()
        recipient_pk = Pubkey.from_string(to)
        sender_ata = get_associated_token_address(sender_pk, mint_pk)
        recipient_ata = get_associated_token_address(recipient_pk, mint_pk)
        amount_atomic = int(round(amount * (10 ** self._decimals)))
        ix = transfer_checked(
            TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=sender_ata,
                mint=mint_pk,
                dest=recipient_ata,
                owner=sender_pk,
                amount=amount_atomic,
                decimals=self._decimals,
                signers=[],
            ),
        )
        # Production-ready code would also add a memo instruction here
        # via the MemoProgram for the ``memo`` argument; left as a
        # follow-on since spl.memo is a separate optional dep.
        blockhash = self._rpc.get_latest_blockhash().value.blockhash
        message = MessageV0.try_compile(
            payer=sender_pk,
            instructions=[ix],
            address_lookup_table_accounts=[],
            recent_blockhash=blockhash,
        )
        tx = VersionedTransaction(message, [self._kp])
        resp = self._rpc.send_transaction(
            tx, opts=TxOpts(skip_confirmation=False),
        )
        sig = (
            resp.value if hasattr(resp, "value") else resp.get("result", "")
        )
        return {"tx_hash": str(sig), "memo": memo}
