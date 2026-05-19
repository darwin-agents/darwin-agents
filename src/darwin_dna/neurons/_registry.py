"""Boots the initial neuron catalog (docs/engineering/04, 18).

Layer 0 ships a deliberately *incomplete* catalog. The Darwin is expected to
discover gaps and file capability requests (Path B) — that is a feature, not a
deficiency. content.publisher / analytics.measurer / accounts.manager etc.
arrive in later phases; until then the orchestrator falls back to
browser.operator and files requests.

The catalog is constructed here, in the genome, with platform-provided ports
injected. Every neuron passes the load-time contract check before it can be
dispatched.
"""

from __future__ import annotations

from ._base import Neuron
from .browser_operator import BrowserOperator
from .content_writer import ContentWriter
from .infrastructure_provisioner import InfrastructureProvisioner, ProvisionPort
from .payments_facilitator import PaymentsFacilitator, WalletPort
from .vault_writer import VaultWriter

# Capabilities the founding strategy wants but Layer 0 does not ship. The
# orchestrator turns an unmet required tag into a structured capability
# request (Path B) instead of failing.
KNOWN_GAPS = frozenset(
    {"content.publish_api", "analytics.measure", "accounts.manage", "ads.manage"}
)


def build_catalog(
    *, wallet: WalletPort, provision: ProvisionPort
) -> dict[str, Neuron]:
    """Instantiate, wire, and validate the founding catalog."""
    # Imported lazily: the loader lives in machinery, which imports the
    # neuron contract from here — a top-level import would close that loop.
    from ..machinery.neuron_loader import load_catalog

    neurons = [
        ContentWriter(),
        BrowserOperator(),
        PaymentsFacilitator(wallet=wallet),
        VaultWriter(),
        InfrastructureProvisioner(provision=provision),
    ]
    return load_catalog(neurons)
