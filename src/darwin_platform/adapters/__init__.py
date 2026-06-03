"""Real provider adapters (docs/engineering/13–17).

The genome speaks in protocols (LLMTransport, WalletPort, etc.); these
adapters connect those protocols to real services. Each adapter is
optional — the platform extra (``pip install darwin-dna[platform]``)
brings in the deps needed to import them; tests skip when the deps are
missing so a slim install still runs the suite.

Currently implemented:
  - ``LiteLLMTransport`` (doc 13): real LLM gateway via the BerriAI
    LiteLLM client, with per-tier model fallback chains and markup-based
    billing into the Darwin's wallet.
"""

from .browser import BrowserClient, BrowserUseClient
from .coinbase_cdp import CdpChainClient, build_cdp_chain_client
from .hvac_vault import HvacVaultClient
from .litellm_admin import (
    LiteLLMAdminClient,
    LiteLLMAdminError,
    LiteLLMSpendReconciler,
    LLMSpendReconciliation,
    ledger_llm_spend_usdc,
)
from .litellm_transport import LiteLLMTransport, TierMapping
from .vault_namespace import (
    RealVaultNamespaceBackend,
    VaultNamespaceError,
    build_vault_namespace_backend,
)
from .sandbox import E2BSandbox, SandboxClient
from .vault_backend import (
    InMemoryVaultClient,
    RealVaultBackend,
    VaultClient,
    VaultError,
)
from .wallet import ChainClient, RealWallet, WalletError
from .web3_chain import (
    USDC_BASE_MAINNET,
    USDC_BASE_SEPOLIA,
    Web3ChainClient,
)
from .x402 import HTTPClient, X402Error, X402PaymentClient

__all__ = [
    "LiteLLMTransport",
    "TierMapping",
    "RealVaultNamespaceBackend",
    "VaultNamespaceError",
    "build_vault_namespace_backend",
    "LiteLLMAdminClient",
    "LiteLLMAdminError",
    "LiteLLMSpendReconciler",
    "LLMSpendReconciliation",
    "ledger_llm_spend_usdc",
    "ChainClient",
    "RealWallet",
    "WalletError",
    "Web3ChainClient",
    "CdpChainClient",
    "build_cdp_chain_client",
    "USDC_BASE_MAINNET",
    "USDC_BASE_SEPOLIA",
    "VaultClient",
    "VaultError",
    "RealVaultBackend",
    "InMemoryVaultClient",
    "HvacVaultClient",
    "BrowserClient",
    "BrowserUseClient",
    "SandboxClient",
    "E2BSandbox",
    "HTTPClient",
    "X402Error",
    "X402PaymentClient",
]
