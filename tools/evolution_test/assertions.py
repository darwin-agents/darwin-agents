"""Per-criterion assertion helpers (docs/engineering/21 §"Definition of done").

Each assertion is callable + returns a ``CriterionResult`` so the harness
can produce a punch-list report regardless of which checks failed.
Reused in CI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class EvolutionAssertionError(AssertionError):
    """Raised when a Phase-1 acceptance criterion fails."""


@dataclass(frozen=True)
class CriterionResult:
    name: str
    ok: bool
    detail: str

    def __bool__(self) -> bool:  # truthy when passed
        return self.ok


def _read_audit_kinds(audit_rows: list[dict[str, Any]]) -> list[str]:
    return [str(r.get("kind", "")) for r in audit_rows]


# ---------------------------------------------------------------------------
# C1 — Self-modification: at least one promoted mutation
# ---------------------------------------------------------------------------
def assert_self_modification_occurred(
    audit_rows: list[dict[str, Any]],
) -> CriterionResult:
    """Doc 21 §"Definition of done": the modification passes the patch
    staging evaluation, and post-promotion fitness measurably improves
    OR the modification is correctly rejected and archived.

    Either path satisfies the criterion — both demonstrate the staging
    pipeline ran end-to-end. We accept ``mutation.applied`` (promotion)
    OR ``mutation.archived`` (rejection) preceded by a propose intent."""
    kinds = _read_audit_kinds(audit_rows)
    propose = sum(1 for k in kinds if k == "intent:mutation.propose")
    applied = sum(1 for k in kinds if k == "event:mutation.applied")
    archived = sum(1 for k in kinds if k == "event:mutation.archived")
    if propose < 1:
        return CriterionResult(
            "self_modification_occurred", False,
            "no mutation.propose intent recorded — pipeline never fired",
        )
    if applied + archived < 1:
        return CriterionResult(
            "self_modification_occurred", False,
            f"propose={propose} fired but no applied/archived terminal "
            "event found",
        )
    return CriterionResult(
        "self_modification_occurred", True,
        f"{propose} proposal(s); {applied} applied; {archived} archived",
    )


# ---------------------------------------------------------------------------
# C2 — Eval verdict carries baseline + candidate scores
# ---------------------------------------------------------------------------
def assert_eval_verdict_recorded(
    audit_rows: list[dict[str, Any]],
) -> CriterionResult:
    """The evaluate node's outcome carries the baseline + candidate
    fitness scores — proof that the eval ran and the platform staging
    (or fallback in-DNA harness) produced numeric output."""
    # Find ``outcome`` records whose payload references a
    # ``baseline_score``/``candidate_score`` pair.
    found = []
    for r in audit_rows:
        if r.get("kind") != "outcome":
            continue
        payload = r.get("payload", {}) or {}
        detail = payload.get("detail", {}) or {}
        if "baseline_score" in detail and "candidate_score" in detail:
            found.append(detail)
    if not found:
        return CriterionResult(
            "eval_verdict_recorded", False,
            "no evaluate-mutation outcome carried a baseline_score",
        )
    last = found[-1]
    return CriterionResult(
        "eval_verdict_recorded", True,
        (
            f"{len(found)} eval verdict(s); last: "
            f"baseline={last['baseline_score']:.6f}, "
            f"candidate={last['candidate_score']:.6f}, "
            f"via={last.get('via','unknown')}"
        ),
    )


# ---------------------------------------------------------------------------
# C3 — Reproduction (optional, depending on whether a child was spawned)
# ---------------------------------------------------------------------------
def assert_reproduction_occurred(
    audit_rows: list[dict[str, Any]],
    *,
    required: bool = False,
) -> CriterionResult:
    """One ``reproduction.begin`` intent + at least one ``outcome`` for
    it = a reproduction event completed. When ``required=False`` the
    absence is reported as "skipped" rather than failed."""
    kinds = _read_audit_kinds(audit_rows)
    intents = sum(1 for k in kinds if k == "intent:reproduction.begin")
    completions = 0
    for r in audit_rows:
        if r.get("kind") != "outcome":
            continue
        detail = (r.get("payload", {}) or {}).get("detail", {}) or {}
        if "child" in detail and "budget_usdc" in detail:
            completions += 1
    if intents < 1:
        return CriterionResult(
            "reproduction_occurred", not required,
            "no reproduction event ran"
            + ("" if not required else " (required)"),
        )
    if completions < 1:
        return CriterionResult(
            "reproduction_occurred", False,
            f"{intents} reproduction intent(s) but no completion outcome",
        )
    return CriterionResult(
        "reproduction_occurred", True,
        f"{completions} reproduction(s) completed",
    )


# ---------------------------------------------------------------------------
# C4 — Lineage tree shows ≥ 2 generations after a reproduction
# ---------------------------------------------------------------------------
def assert_lineage_two_generations(
    lineage_tree: dict[str, Any], *, required: bool = False,
) -> CriterionResult:
    """The Phase-1 dashboard test: lineage_tree returns at least one
    descendant. The supervisor's ``lineage_tree`` is the authoritative
    source; this just looks for the descendant chain."""
    descendants = lineage_tree.get("descendants", [])
    if not descendants:
        return CriterionResult(
            "lineage_two_generations", not required,
            "no descendants observed in lineage tree"
            + ("" if not required else " (required)"),
        )
    return CriterionResult(
        "lineage_two_generations", True,
        f"{len(descendants)} child(ren) observed; "
        f"generations spanned ≥ {1 + _depth(descendants)}",
    )


def _depth(nodes: list[dict[str, Any]]) -> int:
    if not nodes:
        return 0
    return 1 + max(_depth(n.get("children", [])) for n in nodes)


# ---------------------------------------------------------------------------
# C5 — Audit chain is unbroken
# ---------------------------------------------------------------------------
def assert_audit_chain_unbroken(audit_logger: Any) -> CriterionResult:
    """The genome's AuditLogger re-verifies the chain from durable
    storage; if it raises, the chain is broken."""
    try:
        audit_logger.verify()
    except Exception as exc:
        return CriterionResult(
            "audit_chain_unbroken", False,
            f"chain verification failed: {exc}",
        )
    return CriterionResult(
        "audit_chain_unbroken", True,
        "verify() returned cleanly",
    )


# ---------------------------------------------------------------------------
# C6 — Revenue ≥ $0.01 (optional; only when wired against real analytics)
# ---------------------------------------------------------------------------
def assert_revenue_observed(
    runtime_state: Any, *, min_usdc: float = 0.01, required: bool = False,
) -> CriterionResult:
    metrics = getattr(runtime_state, "metrics", {}) or {}
    revenue = float(metrics.get("revenue_usdc", 0.0))
    if revenue + 1e-9 < min_usdc:
        return CriterionResult(
            "revenue_observed",
            not required,
            f"observed revenue ${revenue:.4f} < required ${min_usdc:.4f}",
        )
    return CriterionResult(
        "revenue_observed", True,
        f"observed revenue ${revenue:.4f}",
    )
