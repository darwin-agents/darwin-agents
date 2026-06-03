"""Boots the initial neuron catalog (docs/engineering/04, 18).

Layer 0 ships a curated catalog that covers the major axes of the founding
content+affiliate strategy: write, publish, measure, search, browse, pay,
provision, persist. Each neuron is a Protocol-conformant shell; the
domain-specific backends (publishing API clients, analytics endpoints,
search APIs) are wired through ``NeuronContext.http`` (x402) at runtime.

The Darwin is still expected to discover gaps via the structured capability
request flow (Path B); the catalog being broader just reduces how often the
orchestrator has to escalate "I don't have anything that can do X".
"""

from __future__ import annotations

from ._base import Neuron
from .accounts_manager import AccountsManager
from .ads_manager import AdsManager
from .analytics_measurer import AnalyticsMeasurer
from .browser_operator import BrowserOperator
from .compliance_kyc import ComplianceKycSubmitter
from .content_publisher import ContentPublisher
from .content_writer import ContentWriter
from .infrastructure_provisioner import InfrastructureProvisioner, ProvisionPort
from .payments_facilitator import PaymentsFacilitator, WalletPort
from .research_scout import ResearchScout
from .support_responder import SupportResponder
from .trading_operator import TradingOperator
from .vault_writer import VaultWriter

# Capabilities the founding strategy may want but Layer 0 still does not
# ship as first-class neurons. As of the full catalog expansion the
# original doc-04 founding cohort is fully built; what remains is the
# Path-C (Darwin-generated) extensions a Darwin earns the right to
# author after the earn-before-mutate gate opens.
KNOWN_GAPS = frozenset()


def build_catalog(
    *, wallet: WalletPort, provision: ProvisionPort
) -> dict[str, Neuron]:
    """Instantiate, wire, and validate the founding catalog."""
    # Imported lazily: the loader lives in machinery, which imports the
    # neuron contract from here — a top-level import would close that loop.
    from ..machinery.neuron_loader import load_catalog

    neurons = [
        ContentWriter(),
        ContentPublisher(),
        BrowserOperator(),
        PaymentsFacilitator(wallet=wallet),
        VaultWriter(),
        InfrastructureProvisioner(provision=provision),
        AnalyticsMeasurer(),
        ResearchScout(),
        AccountsManager(),
        AdsManager(),
        TradingOperator(),
        SupportResponder(),
        ComplianceKycSubmitter(),
    ]
    return load_catalog(neurons)
