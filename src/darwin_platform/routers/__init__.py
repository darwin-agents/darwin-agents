"""Darwin Supervisor HTTP routers, grouped by domain (Swagger tags)."""

from __future__ import annotations

from .darwins import router as darwins_router
from .escalations import router as escalations_router
from .stream import router as stream_router
from .ledger import router as ledger_router
from .harvest import router as harvest_router
from .throttle import router as throttle_router
from .fiat import router as fiat_router
from .kyc import router as kyc_router
from .webhooks import router as webhooks_router
from .freelancers import router as freelancers_router
from .me import router as me_router
from .omnibus import router as omnibus_router
from .fleet import router as fleet_router
from .proposals import router as proposals_router
from .vault import router as vault_router
from .wallets import router as wallets_router
from .resources import router as resources_router
from .platform import router as platform_router
from .alerts import router as alerts_router
from .signing import router as signing_router
from .audit import router as audit_router
from .ops import router as ops_router

ALL_ROUTERS = [
    darwins_router,
    escalations_router,
    stream_router,
    ledger_router,
    harvest_router,
    throttle_router,
    fiat_router,
    kyc_router,
    webhooks_router,
    freelancers_router,
    me_router,
    omnibus_router,
    fleet_router,
    proposals_router,
    vault_router,
    wallets_router,
    resources_router,
    platform_router,
    alerts_router,
    signing_router,
    audit_router,
    ops_router,
]
