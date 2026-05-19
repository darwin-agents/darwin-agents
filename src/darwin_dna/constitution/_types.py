"""Shared value objects for the constitution.

What an "action" or a "spend" *is* belongs to the rules of the game, so these
live at the constitution level. Machinery (treasury policy, audit logger)
imports these; the constitution never imports machinery. Dependency arrows
point one way only (docs/engineering/18, file-by-file plan).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ActionKind = Literal[
    "neuron_dispatch",
    "payment",
    "content_publish",
    "trade",
    "escalation",
    "mutation",
    "reproduction",
]

# Coarse counterparty buckets the constitution reasons about. The real, fine
# classification is the platform's job; the genome only needs enough to apply
# its hard rules.
CounterpartyCategory = Literal[
    "infra",  # compute, storage, managed services
    "llm",  # model/inference providers
    "api",  # third-party data/automation APIs
    "ad_network",  # Meta/Google/X ad buying
    "exchange",  # markets / trading / DeFi venues
    "freelancer",  # humans on Upwork/Fiverr/etc.
    "legal_financial",  # lawyers, accountants, financial services
    "owner",  # the Darwin's own owner (harvest/withdrawal)
    "unknown",
]


class SpendIntent(BaseModel):
    """A proposed outbound payment, before the wallet signs anything."""

    model_config = {"frozen": True}

    amount_usdc: float = Field(ge=0)
    counterparty: str
    counterparty_category: CounterpartyCategory = "unknown"
    jurisdiction: str | None = None  # ISO-3166 alpha-2 if known
    category: str  # business category this spend serves
    strategy_id: str | None = None
    est_p95_usdc: float | None = None  # worst-case forward cost of the plan


class ActionDescriptor(BaseModel):
    """Anything side-effecting the orchestrator wants to do, described in
    terms the constitution can screen. Deliberately structured (not free
    text) — the genome must not depend on an LLM to decide if something is
    forbidden."""

    model_config = {"frozen": True}

    kind: ActionKind
    category: str  # the business category in play
    content_tags: frozenset[str] = frozenset()  # e.g. {"csam_suspected"}
    counterparty_category: CounterpartyCategory = "unknown"
    jurisdiction: str | None = None
    touches_owner_vault: bool = False
    impersonates_named_individual: bool = False
    poses_as_licensed_professional: bool = False
    spend: SpendIntent | None = None
