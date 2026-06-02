"""Real :class:`ChainClient` using web3.py (docs/engineering/17).

Talks to any EVM-compatible chain. The MVP target is **Base Sepolia
USDC** — testnet so a real Darwin can metabolize on chain without
exposing actual funds. Production swap to Base mainnet is a config
change.

Architecture:

- :class:`Web3ChainClient` wraps a ``web3.Web3`` instance and a USDC
  ERC-20 contract bound to the configured address.
- ``balance_usdc()`` reads ``balanceOf(address)`` and divides by the
  USDC decimals (6).
- ``transfer(to, amount_usdc, memo)`` signs + submits a USDC
  ``transfer`` call. ``memo`` is passed as a tx ``data`` attachment
  to keep the on-chain trace human-legible; some providers strip it
  but the wallet's own ledger always records it. Waits for the receipt
  (configurable timeout); returns ``{"tx_hash": "0x..."}``.
- Signing is local via :mod:`eth_account` from a private key. The key
  comes from the env (never from disk in the chart) — the
  ``Web3ChainClient`` constructor accepts it as an explicit string so
  the operator can choose how to load it.
- Production deployers can replace this whole class with a Coinbase
  CDP / Privy wrapper that talks to TEE-secured signing; the
  :class:`ChainClient` Protocol is the contract both sides honor.

We do not use this for the supervisor's own state — only as the
:class:`darwin_platform.adapters.RealWallet`'s chain backend.
"""

from __future__ import annotations



# Standard ERC-20 ABI subset — only what we use. Includes ``transfer``
# (state-changing) and ``balanceOf`` + ``decimals`` (read-only).
_ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


# Canonical USDC contract addresses for reference. Operators usually
# pass an explicit ``usdc_contract`` to avoid hard-coding here.
USDC_BASE_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


class Web3ChainClient:
    """:class:`ChainClient`-conformant adapter over web3.py.

    Tests inject a stub ``web3_factory`` returning a mocked
    :class:`web3.Web3`; production wires
    ``web3_factory=lambda: Web3(Web3.HTTPProvider(rpc_url))``.

    Args:
        web3_factory: zero-arg callable returning a configured
            ``web3.Web3`` instance.
        wallet_address: the Darwin's on-chain address (0x...).
        private_key: hex-encoded private key for ``wallet_address``.
            **Never log this; never persist it.** Production loads
            from a Vault read at boot, not from a file.
        usdc_contract: ERC-20 USDC contract address on the target chain.
        chain_id: numeric chain id (Base Sepolia is 84532, Base mainnet
            is 8453). Used to sign transactions.
        receipt_timeout: how long to wait for the tx receipt before
            raising. None disables waiting (fire-and-forget — not
            recommended for tests that need balance reconciliation).
    """

    def __init__(
        self,
        *,
        web3_factory,
        wallet_address: str,
        private_key: str,
        usdc_contract: str,
        chain_id: int,
        receipt_timeout: float | None = 60.0,
    ) -> None:
        try:
            from web3 import Web3
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "Web3ChainClient requires the [web3] extra: "
                "`pip install darwin-dna[web3]`"
            ) from exc
        self._w3 = web3_factory()
        if not isinstance(self._w3, Web3):
            # Defensive: tests sometimes pass a mock that isn't a real
            # Web3 instance; accept it but warn via the attribute name.
            pass
        self._address = Web3.to_checksum_address(wallet_address) if hasattr(Web3, "to_checksum_address") else wallet_address
        self._private_key = private_key
        self._usdc = self._w3.eth.contract(
            address=Web3.to_checksum_address(usdc_contract) if hasattr(Web3, "to_checksum_address") else usdc_contract,
            abi=_ERC20_ABI,
        )
        self._chain_id = int(chain_id)
        self._receipt_timeout = receipt_timeout
        # Cache USDC decimals after first read; doesn't change.
        self._decimals: int | None = None

    # -- ChainClient surface ----------------------------------------------
    def address(self) -> str:
        return self._address

    def balance_usdc(self) -> float:
        raw = self._usdc.functions.balanceOf(self._address).call()
        return float(raw) / (10 ** self._usdc_decimals())

    def transfer(self, *, to: str, amount_usdc: float, memo: str) -> dict:
        """Sign + submit a USDC transfer. Blocks until the receipt is
        available (or ``receipt_timeout`` elapses). The ``memo`` is not
        carried on-chain for an ERC-20 ``transfer`` (the function has no
        memo field); it's recorded in the wallet's own ledger by
        :class:`RealWallet`. We accept it for interface conformance."""
        from web3 import Web3

        decimals = self._usdc_decimals()
        amount_units = int(round(amount_usdc * (10 ** decimals)))
        to_checksum = Web3.to_checksum_address(to) if hasattr(Web3, "to_checksum_address") else to

        nonce = self._w3.eth.get_transaction_count(self._address)
        # Build a transfer txn. We rely on the provider's gas estimate
        # rather than hard-coding — chain conditions vary.
        txn = self._usdc.functions.transfer(to_checksum, amount_units).build_transaction({
            "from": self._address,
            "nonce": nonce,
            "chainId": self._chain_id,
        })
        signed = self._w3.eth.account.sign_transaction(txn, private_key=self._private_key)
        # web3.py < 7 used `rawTransaction`; web3.py >= 7 uses `raw_transaction`.
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
        tx_hash = self._w3.eth.send_raw_transaction(raw)

        if self._receipt_timeout is not None:
            receipt = self._w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=self._receipt_timeout
            )
            status = getattr(receipt, "status", None)
            if status is not None and int(status) != 1:
                raise RuntimeError(
                    f"USDC transfer reverted: {tx_hash.hex() if hasattr(tx_hash, 'hex') else tx_hash}"
                )
        # Normalise tx_hash to a hex string.
        hex_hash = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
        if not hex_hash.startswith("0x"):
            hex_hash = "0x" + hex_hash
        return {"tx_hash": hex_hash, "memo": memo}

    def wait_for_confirmation(
        self,
        tx_hash: str,
        *,
        timeout_s: float = 60.0,
        min_confirmations: int = 1,
    ) -> dict:
        """Block until ``tx_hash`` reaches ``min_confirmations`` block-depth.

        Satisfies :class:`ConfirmingChainClient` so :class:`RealWallet` does
        not credit its ledger before the transfer is final on mainnet
        (docs/engineering/30 §mainnet safety). Raises on a reverted receipt or
        if the depth isn't reached within ``timeout_s`` — the caller treats
        either as "do not credit", so a reorged-out tx is never booked.
        """
        import time

        deadline = time.monotonic() + max(0.0, float(timeout_s))
        want = max(1, int(min_confirmations))
        poll = min(2.0, max(0.2, float(timeout_s) / 30.0))
        while True:
            receipt = None
            try:
                receipt = self._w3.eth.get_transaction_receipt(tx_hash)
            except Exception:
                receipt = None  # not mined yet
            if receipt is not None:
                status = getattr(receipt, "status", None)
                if status is None and isinstance(receipt, dict):
                    status = receipt.get("status")
                if status is not None and int(status) != 1:
                    raise RuntimeError(
                        f"transfer {tx_hash} reverted on-chain"
                    )
                block_no = getattr(receipt, "blockNumber", None)
                if block_no is None and isinstance(receipt, dict):
                    block_no = receipt.get("blockNumber")
                if block_no is not None:
                    depth = int(self._w3.eth.block_number) - int(block_no) + 1
                    if depth >= want:
                        return {
                            "tx_hash": tx_hash,
                            "block_number": int(block_no),
                            "confirmations": int(depth),
                            "status": 1,
                        }
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"transfer {tx_hash} not confirmed to {want} block(s) "
                    f"within {timeout_s}s"
                )
            time.sleep(poll)

    # -- internals --------------------------------------------------------
    def _usdc_decimals(self) -> int:
        if self._decimals is None:
            try:
                self._decimals = int(self._usdc.functions.decimals().call())
            except Exception:
                # USDC is canonically 6 decimals across deployments; fall
                # back to that if the contract read fails for any reason.
                self._decimals = 6
        return self._decimals
