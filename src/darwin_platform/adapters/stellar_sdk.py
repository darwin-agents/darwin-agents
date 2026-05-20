"""Real Stellar SDK adapter (docs/engineering/17 §fallback chains).

Provides a ``ChainClient``-conforming wrapper around the ``stellar-sdk``
library's payment-op flow. The CLI builds this from
``--stellar-horizon-url`` + ``--stellar-secret``.

USDC on Stellar is the Centre/Circle-issued asset
(``STELLAR_USDC_ISSUER_MAINNET``). The wrapper:

- Reads the sender's account from Horizon for the trustline + balance.
- For an outbound transfer, builds a TransactionBuilder with a
  PaymentOp + memo + the standard 100-stroop fee.
- Signs with the sender's secret, submits via Horizon, returns the
  tx hash.

SDK imports are deferred so a slim build that doesn't have
``stellar-sdk`` installed only fails when this module is actually
used."""

from __future__ import annotations

from typing import Any

from .multichain import STELLAR_USDC_ISSUER_MAINNET, StellarChainClient


def build_stellar_chain_client(
    *,
    horizon_url: str,
    secret: str,
    network: str = "public",
    usdc_issuer: str | None = None,
) -> StellarChainClient:
    """Construct a fully-wired :class:`StellarChainClient` from the
    CLI inputs. ``network`` is 'public' (mainnet) or 'testnet'."""
    try:
        from stellar_sdk import Keypair, Network, Server  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - operator hint
        raise ImportError(
            "Stellar adapter requires the 'stellar-sdk' library: "
            "pip install stellar-sdk"
        ) from exc

    if not horizon_url:
        raise ValueError("horizon_url is required")
    if not secret:
        raise ValueError("secret is required")
    issuer = usdc_issuer or STELLAR_USDC_ISSUER_MAINNET
    network_passphrase = (
        Network.PUBLIC_NETWORK_PASSPHRASE
        if network == "public"
        else Network.TESTNET_NETWORK_PASSPHRASE
    )
    keypair = Keypair.from_secret(secret)
    server = _ServerWithTransfer(
        inner=Server(horizon_url=horizon_url),
        keypair=keypair,
        network_passphrase=network_passphrase,
        usdc_issuer=issuer,
    )
    return StellarChainClient(
        stellar_server=server,
        keypair=keypair,
        usdc_issuer=issuer,
        network_passphrase=network_passphrase,
    )


class _ServerWithTransfer:
    """Wraps ``stellar_sdk.Server`` with a ``transfer_usdc`` method
    that builds + signs + submits a PaymentOp transaction."""

    def __init__(
        self,
        *,
        inner: Any,
        keypair: Any,
        network_passphrase: str,
        usdc_issuer: str,
    ) -> None:
        self._server = inner
        self._kp = keypair
        self._network = network_passphrase
        self._issuer = usdc_issuer

    # accounts() is what StellarChainClient.balance_usdc calls. Just
    # delegate to the inner server.
    def accounts(self):
        return self._server.accounts()

    def transfer_usdc(
        self,
        *,
        to: str,
        amount: float,
        memo: str,
        issuer: str | None = None,
    ) -> dict[str, Any]:
        try:
            from stellar_sdk import (  # type: ignore[import-not-found]
                Asset,
                Memo,
                TransactionBuilder,
            )
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "Stellar transfer requires stellar-sdk: "
                "pip install stellar-sdk"
            ) from exc

        source = self._server.load_account(self._kp.public_key)
        asset = Asset("USDC", issuer or self._issuer)
        tx = (
            TransactionBuilder(
                source_account=source,
                network_passphrase=self._network,
                base_fee=100,
            )
            .append_payment_op(
                destination=to, asset=asset, amount=str(round(amount, 7)),
            )
            .add_memo(Memo.text(memo[:28] if memo else ""))
            .set_timeout(120)
            .build()
        )
        tx.sign(self._kp)
        resp = self._server.submit_transaction(tx)
        # Horizon responds with {"hash": ..., ...} when successful.
        return {
            "tx_hash": str(
                resp.get("hash") if isinstance(resp, dict)
                else getattr(resp, "hash", "")
            ),
            "memo": memo,
        }
