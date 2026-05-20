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

from .litellm_transport import LiteLLMTransport, TierMapping

__all__ = ["LiteLLMTransport", "TierMapping"]
