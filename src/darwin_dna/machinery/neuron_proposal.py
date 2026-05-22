"""Path-C: Darwin-generated neurons (docs/engineering/04 phase 3, doc 03).

The earn-before-mutate gate already lets a Darwin propose a behaviour
delta (``BehaviorDelta`` in :mod:`eval_harness`). Path C extends that
to *new neurons*: a Darwin proposes a structured ``NeuronProposal``,
the eval harness runs it under sandbox, and — only if it passes both
the contract validator AND beats the no-neuron baseline by the fitness
margin — the admission gate adds an :class:`InterpretedNeuron` to the
catalog.

The proposal is intentionally narrow:
- ``name`` must be ``"learned.<slug>"`` so admitted neurons cannot
  impersonate the founding catalog.
- ``capability_tags`` MUST be on the allow-list — ``treasury.write``
  and any tag carrying a constitution-level risk are forbidden.
- ``steps`` is a list of strings the interpreted neuron executes via
  ``ctx.sandbox`` (for code-running) or ``ctx.http`` (for outbound),
  capped at 5 steps + per-step cost cap.

The interpreted neuron's `execute` runs the steps in order, returning
the aggregate outcome. It's deliberately limited — production
deployers can swap in a richer interpreter behind the same Protocol.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..neurons._base import (
    BaseNeuron,
    CostEstimate,
    FailureMode,
    NeuronContext,
    NeuronResult,
    Plan,
    Task,
)


# Allow-list of capability tags a Darwin-authored neuron may declare.
# These are the safe, non-treasury-write tags; expanding this list is
# a constitutional change (amendment) — not a soma change.
PATH_C_ALLOWED_TAGS = frozenset({
    "web.read",
    "web.operate",
    "web.publish",
    "content.summarize",
    "content.classify",
    "analytics.measure",
    "research.scout",
    "compute.run",   # sandbox-only computation
})

# Hard limits on a generated neuron's spec.
MAX_PROPOSAL_STEPS = 5
MAX_STEP_LEN = 500
MAX_PROPOSAL_COST_USDC = 0.50   # p95 ceiling


class NeuronProposalError(Exception):
    """Raised by :func:`validate_proposal` on contract failure."""


class NeuronProposal(BaseModel):
    """A structured proposal for a new neuron. Path-C is the ONLY way
    a Darwin can introduce a neuron into its catalog; arbitrary code
    paths must go through this gate."""

    model_config = {"frozen": True}

    name: str = Field(min_length=8, max_length=64)
    rationale: str = Field(min_length=8, max_length=1024)
    capability_tags: frozenset[str]
    steps: tuple[str, ...]
    est_cost_usdc_p50: float = Field(ge=0)
    est_cost_usdc_p95: float = Field(ge=0)

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "rationale": self.rationale,
            "capability_tags": sorted(self.capability_tags),
            "steps": list(self.steps),
            "est_cost_usdc_p50": self.est_cost_usdc_p50,
            "est_cost_usdc_p95": self.est_cost_usdc_p95,
        }


def validate_proposal(
    proposal: NeuronProposal,
    *,
    existing_catalog: dict[str, Any],
) -> None:
    """Contract validator. Refuses any proposal that:

    - names something already in the catalog
    - uses a name prefix other than ``learned.``
    - declares any tag outside :data:`PATH_C_ALLOWED_TAGS`
    - has more than :data:`MAX_PROPOSAL_STEPS` steps
    - has a step longer than :data:`MAX_STEP_LEN`
    - declares p95 above :data:`MAX_PROPOSAL_COST_USDC`
    - p50 > p95 (incoherent estimate)
    """
    if not proposal.name.startswith("learned."):
        raise NeuronProposalError(
            f"Path-C neurons must be named 'learned.<slug>', "
            f"got {proposal.name!r}"
        )
    if proposal.name in existing_catalog:
        raise NeuronProposalError(
            f"name collision: {proposal.name!r} already in catalog"
        )
    if not proposal.capability_tags:
        raise NeuronProposalError(
            "capability_tags is empty; declare at least one tag"
        )
    forbidden = proposal.capability_tags - PATH_C_ALLOWED_TAGS
    if forbidden:
        raise NeuronProposalError(
            f"forbidden tags for Path-C: {sorted(forbidden)}"
        )
    if not proposal.steps:
        raise NeuronProposalError("steps is empty")
    if len(proposal.steps) > MAX_PROPOSAL_STEPS:
        raise NeuronProposalError(
            f"too many steps ({len(proposal.steps)} > {MAX_PROPOSAL_STEPS})"
        )
    for s in proposal.steps:
        if not s.strip():
            raise NeuronProposalError("empty step")
        if len(s) > MAX_STEP_LEN:
            raise NeuronProposalError(
                f"step longer than {MAX_STEP_LEN} chars"
            )
    if proposal.est_cost_usdc_p95 > MAX_PROPOSAL_COST_USDC:
        raise NeuronProposalError(
            f"est_cost_usdc_p95 ({proposal.est_cost_usdc_p95}) > "
            f"{MAX_PROPOSAL_COST_USDC}"
        )
    if proposal.est_cost_usdc_p50 > proposal.est_cost_usdc_p95:
        raise NeuronProposalError(
            f"p50 ({proposal.est_cost_usdc_p50}) > p95 "
            f"({proposal.est_cost_usdc_p95})"
        )


class InterpretedNeuron(BaseNeuron):
    """A neuron whose behaviour is the linear execution of a proposal's
    ``steps``. Each step is dispatched to a substrate based on its
    prefix:

    - ``"http GET <url>"`` / ``"http POST <url>"`` → ctx.http.request
    - ``"sandbox <language> <code>"`` → ctx.sandbox.run_code
    - anything else → escalate (the interpreter is conservative)

    The first failure terminates execution and returns that failure's
    error_code; subsequent steps are skipped."""

    version = "1.0.0"
    failure_modes = [
        FailureMode(
            code="interpreted_step_failed",
            description="a step in the interpreted neuron failed",
            escalation_hint="not human-actionable; the harness will demote",
        ),
        FailureMode(
            code="missing_substrate",
            description="a step required a substrate the context didn't provide",
            escalation_hint="owner adds the substrate or removes the neuron",
        ),
    ]

    def __init__(self, proposal: NeuronProposal):
        self.proposal = proposal
        self.name = proposal.name
        self.capability_tags = proposal.capability_tags
        self.cost_estimate = CostEstimate(
            usd_p50=proposal.est_cost_usdc_p50,
            usd_p95=proposal.est_cost_usdc_p95,
            latency_p50_ms=2000,
            latency_p95_ms=10000,
            escalation_probability=0.05,
        )

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        return Plan(
            steps=list(self.proposal.steps),
            est_cost=self.cost_estimate,
            notes=f"interpreted from proposal {self.proposal.name}",
            payload=dict(task.payload),
        )

    async def execute(
        self, plan: Plan, context: NeuronContext,
    ) -> NeuronResult:
        intent = context.audit.log_intent(
            f"neuron.{self.name}",
            {"steps": list(plan.steps),
             "tags": sorted(self.capability_tags)},
        )
        results: list[dict] = []
        for step in plan.steps:
            outcome = self._dispatch_step(step, context)
            results.append({"step": step[:80], "outcome": outcome})
            if not outcome.get("ok"):
                context.audit.log_outcome(
                    intent, "fail",
                    {"step": step[:80],
                     "reason": outcome.get("reason", "")[:200]},
                )
                code = (
                    "missing_substrate"
                    if outcome.get("reason") == "no substrate"
                    else "interpreted_step_failed"
                )
                return NeuronResult(
                    ok=False, error_code=code,
                    output={"results": results, "failed_step": step[:80]},
                )
        context.audit.log_outcome(
            intent, "ok",
            {"steps_executed": len(plan.steps)},
        )
        return NeuronResult(
            ok=True,
            cost_actual_usdc=self.cost_estimate.usd_p50,
            output={"results": results, "steps_executed": len(plan.steps)},
        )

    # -- step dispatch ---------------------------------------------------
    def _dispatch_step(
        self, step: str, context: NeuronContext,
    ) -> dict:
        s = step.strip()
        low = s.lower()
        if low.startswith(("http get", "http post")):
            return self._http_step(s, context)
        if low.startswith("sandbox "):
            return self._sandbox_step(s, context)
        return {
            "ok": False,
            "reason": (
                "unknown step prefix; supported: 'http GET <url>', "
                "'http POST <url>', 'sandbox <language> <code>'"
            ),
        }

    def _http_step(self, step: str, context: NeuronContext) -> dict:
        http = getattr(context, "http", None)
        if http is None:
            return {"ok": False, "reason": "no substrate"}
        parts = step.split(maxsplit=2)
        if len(parts) < 3:
            return {"ok": False, "reason": "malformed http step"}
        method = parts[1].upper()
        url = parts[2]
        try:
            resp = http.request(method, url)
            status = int(getattr(resp, "status_code", 0))
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}
        if 200 <= status < 400:
            return {"ok": True, "status": status}
        return {"ok": False, "reason": f"http {status}"}

    def _sandbox_step(self, step: str, context: NeuronContext) -> dict:
        sandbox = getattr(context, "sandbox", None)
        if sandbox is None:
            return {"ok": False, "reason": "no substrate"}
        parts = step.split(maxsplit=2)
        if len(parts) < 3:
            return {"ok": False, "reason": "malformed sandbox step"}
        language = parts[1]
        code = parts[2]
        try:
            result = sandbox.run_code(
                language=language, code=code, timeout_seconds=10.0,
            )
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}
        if result.get("ok") and result.get("exit_code", 0) == 0:
            return {
                "ok": True,
                "stdout": (result.get("stdout") or "")[:200],
            }
        return {
            "ok": False,
            "reason": (result.get("stderr") or "non-zero exit")[:200],
        }


# ---------------------------------------------------------------------------
# Admission gate — owns the lifecycle of accepted proposals
# ---------------------------------------------------------------------------

class NeuronAdmissionGate:
    """Tracks pending + admitted proposals. The orchestrator's
    propose_mutation / evaluate_mutation pair drives this; the gate
    is platform-side bookkeeping with audit visibility.

    Admission is empirical: a proposal is admitted only when an
    eval-harness run shows it beats the no-neuron baseline by the
    fitness margin. The gate does not run that eval itself — it
    holds the lifecycle state."""

    def __init__(self, *, max_admitted: int = 16) -> None:
        self._pending: dict[str, NeuronProposal] = {}
        self._admitted: dict[str, InterpretedNeuron] = {}
        self._max_admitted = int(max_admitted)

    def submit(
        self,
        proposal: NeuronProposal,
        *,
        existing_catalog: dict[str, Any],
    ) -> None:
        validate_proposal(proposal, existing_catalog=existing_catalog)
        if proposal.name in self._admitted:
            raise NeuronProposalError(
                f"{proposal.name!r} already admitted"
            )
        self._pending[proposal.name] = proposal

    def pending(self) -> list[NeuronProposal]:
        return list(self._pending.values())

    def admitted(self) -> dict[str, InterpretedNeuron]:
        return dict(self._admitted)

    def admit(self, name: str) -> InterpretedNeuron:
        proposal = self._pending.pop(name, None)
        if proposal is None:
            raise NeuronProposalError(
                f"no pending proposal named {name!r}"
            )
        if len(self._admitted) >= self._max_admitted:
            raise NeuronProposalError(
                f"admitted-neuron cap reached ({self._max_admitted})"
            )
        n = InterpretedNeuron(proposal)
        self._admitted[name] = n
        return n

    def reject(self, name: str, *, reason: str) -> None:
        self._pending.pop(name, None)

    def revoke(self, name: str) -> None:
        """Operator-driven removal of a previously-admitted neuron."""
        self._admitted.pop(name, None)
