"""OpenAPI/Swagger copy for the Darwin Supervisor API.

Centralises the human-facing documentation so it lives in one reviewable
place instead of being scattered across 100+ route decorators:

* ``TAGS``      — one blurb per controller (router), shown under each group
                  heading in Swagger UI. Order here drives the sidebar order.
* ``ENDPOINTS`` — per-operation ``(summary, description)``. The summary is the
                  short label on the collapsed row; the description is the
                  expandable text. ``apply_openapi_metadata`` sets the summary
                  on every matched route and the description only when the
                  handler has no docstring (so the richer hand-written
                  docstrings already on some handlers win).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

# -- top-level API description (rendered as markdown in Swagger) ------------
DESCRIPTION = """\
The **Darwin Supervisor** is the sovereign control plane of the Darwin
ecosystem — the single trusted authority that governs a fleet of autonomous,
self-funding AI organisms ("Darwins").

Each Darwin runs its own signed genome, on-chain wallet, and neurons, and acts
independently. The Supervisor sits above them all: it **spawns and funds** new
organisms, **observes** their anatomy, lineage, escalations, and finances in
real time, and holds powers no Darwin can override —

- the **graded kill switch** (pause → quarantine → terminate), per organism or
  across the entire fleet;
- **treasury governance** — spend caps, spend-locks, wallet policies, and owner
  pre-approvals enforced *before* money moves;
- **signed genome amendments**, shipped through forced reproduction after human
  review;
- a permanent, hash-chained **audit trail** of every operator action.

It is the one surface from which the ecosystem is created, supervised,
constrained, and — when necessary — stopped.
"""

# -- controller (tag) descriptions -----------------------------------------
TAGS: list[dict[str, str]] = [
    {
        "name": "Darwins",
        "description": (
            "Spawn, inspect, and steer individual Darwin organisms: lifecycle "
            "(spawn / pause / resume / kill / force-reproduce), owner safety "
            "controls, anatomy, lineage, and the event timeline."
        ),
    },
    {
        "name": "Escalations",
        "description": (
            "Human-in-the-loop approval tickets a Darwin raises when it needs "
            "owner consent. List them and respond (approve / deny / supply data)."
        ),
    },
    {
        "name": "Stream",
        "description": (
            "WebSocket live stream of a Darwin's anatomy, status, timeline "
            "events, and escalations — the dashboard's real-time feed. (Not "
            "shown as REST operations; connect to the documented WS path.)"
        ),
    },
    {
        "name": "Ledger & Billing",
        "description": (
            "Per-Darwin transaction ledger (JSON / CSV), spend summaries, and "
            "platform billing / revenue rollups."
        ),
    },
    {
        "name": "Harvest",
        "description": (
            "Move profit out of a Darwin's wallet — one-off harvests and "
            "recurring balance-triggered harvest rules."
        ),
    },
    {
        "name": "Throttle",
        "description": (
            "Rate-limit a Darwin by capping daily calls to its LLM tiers and "
            "resource neurons (sandbox / browser / egress)."
        ),
    },
    {
        "name": "Fiat",
        "description": (
            "On/off-ramp between fiat and a Darwin's USDC wallet: deposit "
            "quotes, deposit confirmation, withdrawals, and Darwin→human payouts."
        ),
    },
    {
        "name": "KYC",
        "description": (
            "Identity verification for owners before fiat movement: start an "
            "inquiry and check its status."
        ),
    },
    {
        "name": "Webhooks",
        "description": (
            "Inbound provider callbacks (Stripe, MoonPay, KYC). Called by the "
            "payment / identity ramps — not for manual use."
        ),
    },
    {
        "name": "Freelancers",
        "description": (
            "Operator-managed registry of verified fiat payout destinations a "
            "Darwin may pay, scoped per owner."
        ),
    },
    {
        "name": "Me",
        "description": (
            "Self-service endpoints for the authenticated owner: their own "
            "freelancers and Web-Push notification subscriptions."
        ),
    },
    {
        "name": "Omnibus",
        "description": (
            "Platform omnibus (pooled) USDC account: retry a skipped spawn "
            "funding and credit a user's balance for already-settled deposits."
        ),
    },
    {
        "name": "Fleet",
        "description": (
            "Fleet-wide operator controls: pause / resume everything, global "
            "spend-lock, category / counterparty blocks, and quarantine."
        ),
    },
    {
        "name": "Proposals",
        "description": (
            "Self-evolved neuron proposals from a Darwin: list them and "
            "admit / reject after operator review."
        ),
    },
    {
        "name": "Vault",
        "description": (
            "Per-Darwin secret / document vault: store, list, and delete tiered "
            "entries; sweep expired ones fleet-wide."
        ),
    },
    {
        "name": "Wallets",
        "description": (
            "Wallet spend policy and owner pre-approval rules that authorise a "
            "Darwin's payouts without a fresh escalation."
        ),
    },
    {
        "name": "Resources",
        "description": (
            "Provisioned tenant resources (databases, browser sessions): "
            "inspect and deprovision per Darwin or across the fleet."
        ),
    },
    {
        "name": "Platform",
        "description": (
            "Cross-cutting platform operations: capabilities, pod forensics, "
            "the organism data browser, SMS codes, and amendment reproduction."
        ),
    },
    {
        "name": "Alerts",
        "description": (
            "Monitor alerts raised for a Darwin or across the fleet, filterable "
            "by severity (info / warn / critical)."
        ),
    },
    {
        "name": "Signing",
        "description": (
            "Platform request-signing key management: rotate the key and fetch "
            "the current public key."
        ),
    },
    {
        "name": "Audit & Staging",
        "description": (
            "Read-only operator views: the hash-chained audit log, patch-"
            "staging status / history, and a full fleet snapshot."
        ),
    },
    {
        "name": "Ops",
        "description": (
            "Unauthenticated operational probes: liveness (/healthz), "
            "readiness / leader (/readyz), and Prometheus metrics (/metrics)."
        ),
    },
]

# -- per-endpoint copy ------------------------------------------------------
# key: (HTTP method, route path)  ->  (summary, description | None)
# description=None means the handler already has a docstring we keep.
ENDPOINTS: dict[tuple[str, str], tuple[str, str | None]] = {
    # Darwins
    ("POST", "/darwins"): (
        "Spawn a Darwin",
        "Create and boot a new Darwin organism, optionally funded with opening "
        "USDC and parented to an existing Darwin. Requires owner scope; in "
        "production also requires accepting the kill-switch terms and an 18+ "
        "attestation.",
    ),
    ("GET", "/darwins"): (
        "List Darwins",
        "List Darwin ids the caller may see — the whole fleet for platform "
        "operators, only owned Darwins for an owner.",
    ),
    ("GET", "/darwins/{darwin_id}"): (
        "Get Darwin status",
        "Current runtime status of a Darwin: alive / paused / killed, balance, "
        "tick, and key state.",
    ),
    ("POST", "/darwins/{darwin_id}/pause"): (
        "Pause a Darwin",
        "Pause a Darwin's drive loop (graded kill-switch). It stops acting but "
        "stays warm and can be resumed.",
    ),
    ("POST", "/darwins/{darwin_id}/resume"): (
        "Resume a Darwin",
        "Resume a previously paused Darwin's drive loop.",
    ),
    ("POST", "/darwins/{darwin_id}/kill"): (
        "Kill a Darwin",
        "Permanently terminate a Darwin (graded kill-switch). Irreversible — "
        "the organism is reaped and cannot resume.",
    ),
    ("POST", "/darwins/{darwin_id}/reproduce/force"): (
        "Force reproduction",
        "Force this Darwin to reproduce now, minting a child that inherits its "
        "genome (operator / owner action).",
    ),
    ("POST", "/darwins/{darwin_id}/controls/spend_lock"): (
        "Set spend lock",
        "Lock or unlock all outbound spending for this Darwin (owner soft "
        "kill-switch for money).",
    ),
    ("POST", "/darwins/{darwin_id}/controls/reproduction_block"): (
        "Set reproduction block",
        "Block or unblock this Darwin from reproducing.",
    ),
    ("POST", "/darwins/{darwin_id}/controls/disabled_neurons"): (
        "Set disabled neurons",
        "Replace the set of neurons disabled for this Darwin (it will skip "
        "dispatching them).",
    ),
    ("POST", "/darwins/{darwin_id}/controls/disabled_categories"): (
        "Set disabled categories",
        "Replace the set of spend categories this Darwin is forbidden from "
        "transacting in.",
    ),
    ("GET", "/darwins/{darwin_id}/controls"): (
        "Get owner controls",
        "Current owner safety controls for a Darwin: spend-lock, "
        "reproduction-block, disabled neurons, and disabled categories.",
    ),
    ("GET", "/darwins/{darwin_id}/anatomy"): ("Get Darwin anatomy", None),
    ("GET", "/darwins/{darwin_id}/lineage"): (
        "Get Darwin lineage",
        "Ancestry of this Darwin — the chain of parent reproductions that "
        "produced it.",
    ),
    ("GET", "/darwins/{darwin_id}/lineage/tree"): ("Get lineage tree", None),
    ("GET", "/darwins/{darwin_id}/events"): (
        "Get event timeline",
        "Paginated timeline of events for this Darwin (state changes, neuron "
        "dispatches, escalations).",
    ),
    # Escalations
    ("POST", "/darwins/{darwin_id}/escalations/{escalation_id}/respond"): (
        "Respond to an escalation",
        "Resolve an open escalation ticket for a Darwin: approve, deny, or "
        "supply the requested data.",
    ),
    ("GET", "/darwins/{darwin_id}/escalations"): (
        "List a Darwin's escalations",
        "Open (and optionally resolved) escalation tickets raised by this "
        "Darwin.",
    ),
    ("GET", "/darwins/{darwin_id}/escalations/{escalation_id}"): (
        "Get an escalation",
        "Fetch a single escalation ticket by id.",
    ),
    ("GET", "/escalations"): ("List fleet escalations", None),
    # Ledger & Billing
    ("GET", "/darwins/{darwin_id}/ledger"): (
        "Get transaction ledger",
        "Per-Darwin transaction ledger as JSON.",
    ),
    ("GET", "/darwins/{darwin_id}/ledger.csv"): ("Download ledger as CSV", None),
    ("GET", "/darwins/{darwin_id}/ledger/summary"): ("Get ledger summary", None),
    ("GET", "/darwins/{darwin_id}/billing"): (
        "Get a Darwin's billing",
        "Billing summary for this Darwin — platform markup and harvest fees "
        "attributed to it.",
    ),
    ("GET", "/platform/billing"): (
        "Get fleet billing",
        "Platform-wide billing rollup: total revenue (markup + harvest fees) "
        "across the fleet.",
    ),
    # Harvest
    ("POST", "/darwins/{darwin_id}/harvest"): (
        "Harvest profit",
        "Move a fixed amount of USDC from this Darwin's wallet to a destination "
        "address (one-off profit harvest).",
    ),
    ("POST", "/darwins/{darwin_id}/harvest_rules"): (
        "Add a harvest rule",
        "Create a recurring harvest rule: auto-move profit to a destination "
        "when the balance crosses a trigger, on an interval.",
    ),
    ("GET", "/darwins/{darwin_id}/harvest_rules"): (
        "List harvest rules",
        "Recurring harvest rules installed for this Darwin.",
    ),
    ("DELETE", "/darwins/{darwin_id}/harvest_rules/{rule_id}"): (
        "Delete a harvest rule",
        "Remove a recurring harvest rule by id.",
    ),
    # Throttle
    ("POST", "/platform/darwins/{darwin_id}/resource_throttle"): (
        "Set resource throttle",
        "Cap a Darwin's daily calls to a resource neuron "
        "(sandbox | browser | egress).",
    ),
    ("DELETE", "/platform/darwins/{darwin_id}/resource_throttle"): (
        "Clear resource throttle",
        "Remove a resource-neuron throttle for a Darwin.",
    ),
    ("GET", "/platform/darwins/{darwin_id}/resource_throttle"): (
        "Get resource throttle",
        "Current resource-neuron throttle (cap + usage today) for a Darwin.",
    ),
    ("POST", "/platform/darwins/{darwin_id}/throttle"): (
        "Set LLM throttle",
        "Cap a Darwin's daily calls to an LLM tier "
        "(fast | reasoning | coding | image_gen | embedding).",
    ),
    ("DELETE", "/platform/darwins/{darwin_id}/throttle"): (
        "Clear LLM throttle",
        "Remove an LLM-tier throttle for a Darwin.",
    ),
    ("GET", "/platform/darwins/{darwin_id}/throttle"): (
        "Get LLM throttles",
        "Current LLM-tier throttles (caps + usage today) for a Darwin.",
    ),
    # Fiat
    ("POST", "/platform/fiat/deposit_quote"): (
        "Quote a fiat deposit",
        "Get a quote (fees, USDC out) for depositing fiat via a payment method "
        "toward a Darwin's wallet.",
    ),
    ("POST", "/platform/fiat/confirm_deposit"): (
        "Confirm a fiat deposit",
        "Confirm a previously quoted fiat deposit, crediting USDC to the "
        "destination.",
    ),
    ("POST", "/platform/fiat/withdraw"): (
        "Withdraw to fiat",
        "Off-ramp USDC from the platform to a fiat destination.",
    ),
    ("POST", "/darwins/{darwin_id}/fiat_payment"): ("Pay a human (fiat payout)", None),
    ("POST", "/darwins/{darwin_id}/fiat_payment/refund"): (
        "Refund a failed payout",
        None,
    ),
    # KYC
    ("POST", "/platform/kyc/start"): ("Start a KYC inquiry", None),
    ("GET", "/platform/kyc/status"): ("Get KYC status", None),
    ("POST", "/platform/kyc/webhook"): ("KYC inquiry webhook", None),
    # Webhooks
    ("POST", "/webhooks/stripe"): ("Stripe webhook", None),
    ("POST", "/webhooks/moonpay"): ("MoonPay webhook", None),
    ("POST", "/webhooks/kyc"): ("KYC provider webhook", None),
    # Freelancers
    ("POST", "/platform/freelancers"): (
        "Register a freelancer",
        "Operator-onboard a verified fiat payout destination for an owner.",
    ),
    ("GET", "/platform/freelancers/{owner_subject}"): (
        "List an owner's freelancers",
        "Registered fiat payout destinations for a given owner.",
    ),
    ("DELETE", "/platform/freelancers/{owner_subject}/{freelancer_id}"): (
        "Revoke a freelancer",
        "Remove a fiat payout destination from an owner's registry.",
    ),
    # Me
    ("POST", "/me/freelancers"): (
        "Register my freelancer",
        "Self-service: the authenticated owner registers one of their own fiat "
        "payout destinations.",
    ),
    ("GET", "/me/freelancers"): (
        "List my freelancers",
        "Self-service: the authenticated owner's registered payout destinations.",
    ),
    ("DELETE", "/me/freelancers/{freelancer_id}"): (
        "Revoke my freelancer",
        "Self-service: remove one of the authenticated owner's payout "
        "destinations.",
    ),
    ("GET", "/me/push-vapid-public-key"): (
        "Get push VAPID key",
        "The server's VAPID public key the PWA needs to subscribe to Web Push.",
    ),
    ("POST", "/me/push-subscriptions"): (
        "Register push subscription",
        "Self-service: register a browser Web-Push subscription for "
        "notifications.",
    ),
    ("GET", "/me/push-subscriptions"): (
        "List push subscriptions",
        "Self-service: the authenticated owner's registered Web-Push "
        "subscriptions.",
    ),
    ("DELETE", "/me/push-subscriptions"): (
        "Revoke push subscription",
        "Self-service: unregister a Web-Push subscription (by ?endpoint=).",
    ),
    # Omnibus
    ("POST", "/darwins/{darwin_id}/omnibus/fund"): ("Retry omnibus funding", None),
    ("POST", "/platform/omnibus/credit"): ("Credit omnibus balance", None),
    # Fleet
    ("POST", "/platform/fleet/pause"): (
        "Pause the fleet",
        "Pause every Darwin in the fleet at once (fleet-wide kill-switch step).",
    ),
    ("POST", "/platform/fleet/resume"): (
        "Resume the fleet",
        "Resume every paused Darwin in the fleet.",
    ),
    ("POST", "/platform/fleet/spend_lock"): (
        "Fleet spend lock",
        "Lock or unlock outbound spending across the entire fleet.",
    ),
    ("POST", "/platform/fleet/block_category"): (
        "Block a category fleet-wide",
        "Forbid every Darwin from transacting in a spend category.",
    ),
    ("POST", "/platform/fleet/block_counterparty"): (
        "Block a counterparty fleet-wide",
        "Block an address / counterparty across the entire fleet.",
    ),
    ("POST", "/platform/darwins/{darwin_id}/quarantine"): (
        "Quarantine a Darwin",
        "Isolate a Darwin (graded kill-switch between pause and kill) with an "
        "audited reason.",
    ),
    ("DELETE", "/platform/darwins/{darwin_id}/quarantine"): (
        "Release from quarantine",
        "Lift a Darwin's quarantine.",
    ),
    # Proposals
    ("GET", "/darwins/{darwin_id}/proposals"): (
        "List neuron proposals",
        "Self-evolved neuron proposals this Darwin has staged for operator "
        "review.",
    ),
    ("POST", "/platform/darwins/{darwin_id}/proposals/{name}/admit"): (
        "Admit a neuron proposal",
        "Approve a self-evolved neuron proposal so the Darwin may use it.",
    ),
    ("POST", "/platform/darwins/{darwin_id}/proposals/{name}/reject"): (
        "Reject a neuron proposal",
        "Reject a self-evolved neuron proposal with a reason.",
    ),
    # Vault
    ("POST", "/darwins/{darwin_id}/vault"): (
        "Put a vault entry",
        "Store a tiered secret / document (public | scoped | sensitive) in a "
        "Darwin's vault, optionally with a TTL and watermarking.",
    ),
    ("DELETE", "/darwins/{darwin_id}/vault"): (
        "Delete a vault entry",
        "Remove a vault entry (by key / scope) for a Darwin.",
    ),
    ("GET", "/darwins/{darwin_id}/vault"): (
        "List vault entries",
        "List a Darwin's vault entries (metadata only; values are not "
        "returned).",
    ),
    ("POST", "/platform/vault/sweep"): (
        "Sweep expired vault entries",
        "Fleet-wide retention sweep: purge expired vault entries.",
    ),
    # Wallets
    ("POST", "/platform/wallets/{darwin_id}/policy"): (
        "Set wallet policy",
        "Set spend caps (per-action / per-day / per-lifetime) for a Darwin's "
        "wallet.",
    ),
    ("GET", "/platform/wallets/{darwin_id}/policy"): (
        "Get wallet policy",
        "Current spend-cap policy for a Darwin's wallet.",
    ),
    ("PUT", "/platform/wallets/{darwin_id}/preapprovals/{rule_id}"): (
        "Set a pre-approval rule",
        "Create / replace an owner pre-approval rule that auto-authorises "
        "matching payouts without a fresh escalation.",
    ),
    ("DELETE", "/platform/wallets/{darwin_id}/preapprovals/{rule_id}"): (
        "Revoke a pre-approval rule",
        "Remove an owner pre-approval rule.",
    ),
    ("GET", "/platform/wallets/{darwin_id}/preapprovals"): (
        "List pre-approval rules",
        "Owner pre-approval rules currently set on a Darwin's wallet.",
    ),
    # Resources
    ("GET", "/darwins/{darwin_id}/browser/sessions"): (
        "List browser sessions",
        "Browser-neuron sessions recorded for a Darwin (cost + cache status).",
    ),
    ("GET", "/platform/browser/stats"): (
        "Get browser cache stats",
        "Fleet-wide browser session / cache statistics.",
    ),
    ("GET", "/darwins/{darwin_id}/resources"): (
        "List a Darwin's resources",
        "Provisioned tenant resources (databases, namespaces) for a Darwin.",
    ),
    ("DELETE", "/darwins/{darwin_id}/resources/{resource_id}"): (
        "Deprovision a resource",
        "Tear down a provisioned tenant resource for a Darwin.",
    ),
    ("GET", "/platform/resources"): (
        "List fleet resources",
        "Provisioned tenant resources across the whole fleet.",
    ),
    # Platform
    ("GET", "/platform/capabilities"): (
        "Get platform capabilities",
        "Which adapters / features are live (real vs fake): wallet, LLM, "
        "sandbox, browser, KYC, fiat, etc.",
    ),
    ("GET", "/platform/darwins/{darwin_id}/forensics"): (
        "List pod forensics",
        "Soma forensic snapshots captured for a Darwin's orchestrator pod.",
    ),
    ("GET", "/platform/darwins/{darwin_id}/forensics/{snapshot_id}"): (
        "Read a forensic snapshot",
        "Fetch a single soma forensic snapshot by id.",
    ),
    ("GET", "/platform/darwins/{darwin_id}/organism/tables"): (
        "List organism tables",
        "Public tables in a Darwin's business database, via the read-only "
        "dashboard role.",
    ),
    ("GET", "/platform/darwins/{darwin_id}/organism/tables/{table}/rows"): (
        "Read organism rows",
        "Read rows from a table in a Darwin's business database (SELECT-only).",
    ),
    ("POST", "/platform/darwins/{darwin_id}/reproduce_with_amendment"): (
        "Reproduce with amendment",
        "Ship a platform amendment layer by forcing reproduction into an "
        "amended child, then killing the parent.",
    ),
    ("GET", "/platform/sms/verification-code"): ("Get SMS verification code", None),
    # Alerts
    ("GET", "/darwins/{darwin_id}/alerts"): (
        "List a Darwin's alerts",
        "Monitor alerts raised for this Darwin, optionally filtered by severity "
        "(info | warn | critical).",
    ),
    ("GET", "/platform/alerts"): (
        "List fleet alerts",
        "Monitor alerts across the whole fleet, optionally filtered by severity.",
    ),
    # Signing
    ("POST", "/platform/signing/rotate"): (
        "Rotate signing key",
        "Rotate the platform request-signing key, optionally with a drain "
        "window for the old key.",
    ),
    ("GET", "/platform/signing/public_key"): (
        "Get signing public key",
        "The platform's current request-signing public key.",
    ),
    # Audit & Staging
    ("GET", "/platform/audit"): (
        "Get audit log",
        "The hash-chained operator audit log (append-only), filterable by "
        "Darwin / action.",
    ),
    ("GET", "/platform/staging/history"): (
        "Get staging history",
        "History of patch-staging verdicts (promotions / rejections).",
    ),
    ("GET", "/platform/staging/status"): ("Get staging status", None),
    ("GET", "/platform/snapshot"): (
        "Get fleet snapshot",
        "A full observability snapshot of the fleet for dashboards.",
    ),
    # Ops
    ("GET", "/healthz"): (
        "Liveness probe",
        "Unauthenticated liveness check: process status, persistence backend, "
        "uptime, and Darwin count.",
    ),
    ("GET", "/readyz"): (
        "Readiness probe",
        "Unauthenticated readiness check. Under HA, only the leader pod with a "
        "healthy DB returns 200; followers return 503.",
    ),
    ("GET", "/metrics"): (
        "Prometheus metrics",
        "Unauthenticated Prometheus exposition of fleet, billing, throttle, "
        "vault, and LLM circuit-breaker metrics.",
    ),
}


def apply_openapi_metadata(app: FastAPI) -> None:
    """Stamp ``summary`` (always) and ``description`` (only when the handler
    has no docstring) onto each route from :data:`ENDPOINTS`. Called once in
    ``build_app`` after every router is mounted, before the schema is built."""
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            meta = ENDPOINTS.get((method, route.path))
            if meta is None:
                continue
            summary, description = meta
            if summary:
                route.summary = summary
            if description and not route.description:
                route.description = description
            break
