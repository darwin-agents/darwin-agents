"""Real patch-staging pipeline (docs/engineering/21 §"The patch staging pipeline").

The Layer-0 eval harness (:mod:`darwin_dna.machinery.eval_harness`) runs a
structured :class:`BehaviorDelta` over a deterministic frozen tape — pure
function, no IO. That is sufficient for Phase 0 because the "patch" is a
bounded BehaviorDelta, not real code applied to real files.

Phase 1 upgrades to a **real code-patch staging pipeline**:

1. Provision a fresh sandbox (E2B microVM via the platform's
   :class:`SandboxClient` adapter — or any object honouring its shape).
2. Re-run the in-DNA patch validator (independent of the proposer).
3. Build a :class:`FrozenScenario` from N recent real audit entries
   (not the synthetic trigger-only scenario the in-DNA harness uses).
4. Execute the evaluation harness inside the sandbox; the script returns
   a JSON verdict on its last stdout line.
5. Translate stdout + cost into a :class:`StagingVerdict`.
6. Tear down the sandbox regardless of outcome.

The validator runs **outside** the sandbox: a malicious patch could
otherwise persuade its own validator by hijacking stdout. The eval
runs **inside** so the comparison happens against the same code paths
a production rollout would touch.

Quota: a fleet-wide cap on concurrent staging evaluations
(default 3, configurable per doc 21 §"Sandbox lifecycle"). Out-of-quota
calls return a clear ``promote=False, reason="staging quota exhausted"``
rather than blocking the supervisor's drive loop.

Pure-fallback path (no sandbox factory): the service runs the same
evaluation in-process. This is what tests + the in-memory dev path use;
production wires the E2B factory.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from darwin_dna.hyperparameters import DEFAULT_HYPERPARAMETERS, Hyperparameters
from darwin_dna.machinery.eval_harness import (
    BehaviorDelta,
    FrozenScenario,
    evaluate_mutation_candidate,
)
from darwin_dna.machinery.patch_validator import Patch, validate_patch


@runtime_checkable
class StagingSandbox(Protocol):
    """The minimal sandbox surface :class:`PatchStagingService` needs.

    Compatible with :class:`darwin_platform.adapters.sandbox.SandboxClient`
    by intent — the staging service only needs the one-shot ``run_code``
    method. Production wires :class:`E2BSandbox`; tests inject a stub
    returning a deterministic dict.
    """

    def run_code(
        self,
        *,
        language: str,
        code: str,
        timeout_seconds: float = 30.0,
    ) -> dict: ...


@dataclass(frozen=True)
class StagingVerdict:
    """The output of the staging pipeline. The supervisor uses
    ``promote`` to gate the apply step; ``reason`` is what gets recorded
    on the operator audit log and in the mutation archive.

    ``baseline_score`` / ``candidate_score`` are the fitness numbers the
    in-sandbox harness returned. ``sandbox_cost_usdc`` is the platform's
    billable cost for this staging session (zero for the in-process
    fallback). ``sandbox_session_id`` ties the verdict to the sandbox
    invocation for debugging.
    """

    promote: bool
    reason: str
    baseline_score: float
    candidate_score: float
    sandbox_cost_usdc: float
    sandbox_session_id: str
    failure_class: str
    rejected_path: str | None = None
    ran_in_sandbox: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "promote": self.promote,
            "reason": self.reason,
            "baseline_score": self.baseline_score,
            "candidate_score": self.candidate_score,
            "sandbox_cost_usdc": self.sandbox_cost_usdc,
            "sandbox_session_id": self.sandbox_session_id,
            "failure_class": self.failure_class,
            "rejected_path": self.rejected_path,
            "ran_in_sandbox": self.ran_in_sandbox,
        }


@dataclass
class StagingQuota:
    """Concurrency cap on staging evaluations across the fleet.

    Default ``max_concurrent=3`` per doc 21 §"Sandbox lifecycle". Cheap
    in-memory accounting; the supervisor checks ``acquire`` before
    starting an evaluation and ``release`` after.
    """

    max_concurrent: int = 3
    _active: int = field(default=0, init=False)
    _total_acquired: int = field(default=0, init=False)
    _total_rejected: int = field(default=0, init=False)

    def acquire(self) -> bool:
        if self._active >= self.max_concurrent:
            self._total_rejected += 1
            return False
        self._active += 1
        self._total_acquired += 1
        return True

    def release(self) -> None:
        if self._active > 0:
            self._active -= 1

    @property
    def active(self) -> int:
        return self._active

    def snapshot(self) -> dict[str, int]:
        return {
            "max_concurrent": self.max_concurrent,
            "active": self._active,
            "total_acquired": self._total_acquired,
            "total_rejected": self._total_rejected,
        }


class PatchStagingService:
    """Stage a real code patch in a sandbox and return a promote/reject verdict.

    The service is single-tenant from the genome's perspective (it is
    called between the propose and apply nodes for one Darwin) but
    multi-tenant across the fleet — one shared instance per supervisor,
    sharing one :class:`StagingQuota`. Construction is dependency-injected
    so production wires the E2B adapter while tests pass a stub.

    Failure modes (all return a verdict, never raise):

    - Validator rejects the patch → ``promote=False, reason=...``.
    - Quota exhausted → ``promote=False, reason="staging quota exhausted"``.
    - Sandbox factory raises → ``promote=False, reason="sandbox error: ..."``.
    - Sandbox runs but exits non-zero → ``promote=False`` with stderr in reason.
    - Sandbox stdout doesn't parse as JSON → ``promote=False`` with parse error.
    """

    def __init__(
        self,
        *,
        sandbox_factory: Callable[[], StagingSandbox] | None = None,
        clock: Callable[[], float] = time.time,
        quota: StagingQuota | None = None,
        hp: Hyperparameters = DEFAULT_HYPERPARAMETERS,
        evaluation_timeout_s: float = 30.0,
        history_cap: int = 256,
    ) -> None:
        self._sandbox_factory = sandbox_factory
        self._clock = clock
        self._quota = quota or StagingQuota()
        self._hp = hp
        self._timeout = float(evaluation_timeout_s)
        self._history_cap = max(1, int(history_cap))
        self._history: list[StagingVerdict] = []

    # -- public entry point ------------------------------------------------
    def stage_and_evaluate(
        self,
        *,
        darwin_id: str,
        patch: Patch,
        behavior_delta: BehaviorDelta,
        failure_class: str,
        recurrence: int,
        recent_audit: list[dict[str, Any]] | None = None,
    ) -> StagingVerdict:
        """Run the full pipeline. Always returns a verdict."""
        # Independent re-validation: the propose step's verdict cannot be
        # trusted by the staging service. A patch that targets DNA must
        # be rejected here even if propose somehow let it through.
        decision = validate_patch(patch)
        if not decision.ok:
            return self._record(
                StagingVerdict(
                    promote=False,
                    reason=f"validator rejected: {decision.reason}",
                    baseline_score=0.0, candidate_score=0.0,
                    sandbox_cost_usdc=0.0, sandbox_session_id="",
                    failure_class=failure_class,
                    rejected_path=decision.rejected_path,
                )
            )

        scenario = self._scenario_from_audit(
            failure_class=failure_class,
            recurrence=recurrence,
            recent_audit=recent_audit or [],
        )

        if self._sandbox_factory is None:
            return self._record(self._evaluate_in_process(
                darwin_id=darwin_id, delta=behavior_delta,
                scenario=scenario,
            ))

        if not self._quota.acquire():
            return self._record(
                StagingVerdict(
                    promote=False,
                    reason="staging quota exhausted (concurrent cap reached)",
                    baseline_score=0.0, candidate_score=0.0,
                    sandbox_cost_usdc=0.0, sandbox_session_id="",
                    failure_class=failure_class,
                )
            )

        try:
            return self._record(self._evaluate_in_sandbox(
                darwin_id=darwin_id, delta=behavior_delta,
                scenario=scenario, failure_class=failure_class,
            ))
        finally:
            self._quota.release()

    # -- sandbox execution -------------------------------------------------
    def _evaluate_in_sandbox(
        self,
        *,
        darwin_id: str,
        delta: BehaviorDelta,
        scenario: FrozenScenario,
        failure_class: str,
    ) -> StagingVerdict:
        session_id = f"staging-{darwin_id}-{int(self._clock() * 1000)}"
        try:
            sandbox = self._sandbox_factory()
        except Exception as exc:
            return StagingVerdict(
                promote=False,
                reason=f"sandbox init failed: {exc}",
                baseline_score=0.0, candidate_score=0.0,
                sandbox_cost_usdc=0.0, sandbox_session_id=session_id,
                failure_class=failure_class,
            )

        code = self._build_eval_script(delta=delta, scenario=scenario)
        try:
            result = sandbox.run_code(
                language="python", code=code,
                timeout_seconds=self._timeout,
            )
        except Exception as exc:
            return StagingVerdict(
                promote=False,
                reason=f"sandbox run_code raised: {exc}",
                baseline_score=0.0, candidate_score=0.0,
                sandbox_cost_usdc=0.0, sandbox_session_id=session_id,
                failure_class=failure_class,
            )

        if not isinstance(result, dict):
            return StagingVerdict(
                promote=False,
                reason="sandbox returned a non-dict result",
                baseline_score=0.0, candidate_score=0.0,
                sandbox_cost_usdc=0.0, sandbox_session_id=session_id,
                failure_class=failure_class,
            )
        cost = float(result.get("cost_usdc", 0.0))
        if not result.get("ok"):
            stderr = str(result.get("stderr", ""))[:240]
            return StagingVerdict(
                promote=False,
                reason=f"sandbox eval failed: {stderr or 'no stderr'}",
                baseline_score=0.0, candidate_score=0.0,
                sandbox_cost_usdc=cost,
                sandbox_session_id=session_id,
                failure_class=failure_class,
                ran_in_sandbox=True,
            )

        stdout = str(result.get("stdout", "")).strip()
        if not stdout:
            return StagingVerdict(
                promote=False,
                reason="sandbox returned empty stdout",
                baseline_score=0.0, candidate_score=0.0,
                sandbox_cost_usdc=cost,
                sandbox_session_id=session_id,
                failure_class=failure_class,
                ran_in_sandbox=True,
            )
        # The script prints exactly one JSON object — read the last line so
        # incidental sandbox banners (e.g. e2b startup messages) are ignored.
        last_line = stdout.splitlines()[-1]
        try:
            verdict_json = json.loads(last_line)
        except Exception as exc:
            return StagingVerdict(
                promote=False,
                reason=f"sandbox returned unparseable verdict: {exc}",
                baseline_score=0.0, candidate_score=0.0,
                sandbox_cost_usdc=cost,
                sandbox_session_id=session_id,
                failure_class=failure_class,
                ran_in_sandbox=True,
            )
        return StagingVerdict(
            promote=bool(verdict_json.get("promote", False)),
            reason=str(verdict_json.get("reason", "")),
            baseline_score=float(verdict_json.get("baseline_score", 0.0)),
            candidate_score=float(verdict_json.get("candidate_score", 0.0)),
            sandbox_cost_usdc=cost,
            sandbox_session_id=session_id,
            failure_class=failure_class,
            ran_in_sandbox=True,
        )

    # -- in-process fallback ----------------------------------------------
    def _evaluate_in_process(
        self,
        *,
        darwin_id: str,
        delta: BehaviorDelta,
        scenario: FrozenScenario,
    ) -> StagingVerdict:
        """No sandbox factory wired — run the harness in this process. The
        validator already passed at the staging entry; the harness's
        ``validator_clean=True`` argument reflects that."""
        result = evaluate_mutation_candidate(
            delta, scenario, hp=self._hp, validator_clean=True,
        )
        return StagingVerdict(
            promote=result.promote,
            reason=result.reason,
            baseline_score=result.baseline.score,
            candidate_score=result.candidate.score,
            sandbox_cost_usdc=0.0,
            sandbox_session_id=f"in_process-{darwin_id}",
            failure_class=delta.failure_class,
            ran_in_sandbox=False,
        )

    # -- scenario builder --------------------------------------------------
    def _scenario_from_audit(
        self,
        *,
        failure_class: str,
        recurrence: int,
        recent_audit: list[dict[str, Any]],
    ) -> FrozenScenario:
        """Build a :class:`FrozenScenario` from real audit entries.

        Every ``outcome`` record whose detail names this failure class
        becomes a ``"fail"`` step; every clean ``outcome`` becomes an
        ``"ok"`` step. When the audit history is empty (e.g. a brand-new
        Darwin or a forced staging call) we fall back to
        :meth:`FrozenScenario.from_trigger`, which is exactly what the
        Layer-0 in-DNA evaluator does — preserves Phase-0 behaviour.
        """
        tape: list[str] = []
        for rec in recent_audit:
            kind = str(rec.get("kind", ""))
            payload = rec.get("payload", {}) or {}
            if kind.startswith("outcome"):
                status = str(payload.get("status", ""))
                detail = payload.get("detail", {}) or {}
                # The genome's neuron dispatch path records the failure
                # class on the outcome detail under either name.
                err = (
                    detail.get("error_code")
                    or detail.get("failure_class")
                    or ""
                )
                if status == "ok":
                    tape.append("ok")
                elif err == failure_class:
                    tape.append("fail")
                # Other outcomes (different failure class) are ignored —
                # the scenario is scoped to this one class.
        if not tape:
            return FrozenScenario.from_trigger(failure_class, recurrence)
        return FrozenScenario(failure_class=failure_class, tape=tuple(tape))

    # -- eval-script builder -----------------------------------------------
    def _build_eval_script(
        self,
        *,
        delta: BehaviorDelta,
        scenario: FrozenScenario,
    ) -> str:
        """Return a self-contained Python script that runs the in-DNA
        harness against the given delta + scenario and prints a JSON
        verdict on stdout.

        The script imports from ``darwin_dna`` so the sandbox image
        must have the genome's wheel installed; in tests we point the
        sandbox at the current interpreter so the import works
        out-of-the-box.
        """
        delta_payload = json.dumps(delta.model_dump(mode="json"))
        scenario_payload = json.dumps(scenario.model_dump(mode="json"))
        return (
            "import json\n"
            "from darwin_dna.machinery.eval_harness import (\n"
            "    BehaviorDelta, FrozenScenario, evaluate_mutation_candidate\n"
            ")\n"
            f"delta = BehaviorDelta(**json.loads({delta_payload!r}))\n"
            f"scenario = FrozenScenario(**json.loads({scenario_payload!r}))\n"
            "result = evaluate_mutation_candidate(\n"
            "    delta, scenario, validator_clean=True,\n"
            ")\n"
            "print(json.dumps({\n"
            "    'promote': result.promote,\n"
            "    'reason': result.reason,\n"
            "    'baseline_score': result.baseline.score,\n"
            "    'candidate_score': result.candidate.score,\n"
            "}))\n"
        )

    # -- bookkeeping -------------------------------------------------------
    def _record(self, v: StagingVerdict) -> StagingVerdict:
        self._history.append(v)
        if len(self._history) > self._history_cap:
            # Drop the oldest; staging history is forensic, not the audit log.
            del self._history[: len(self._history) - self._history_cap]
        return v

    def history(self, *, limit: int | None = None) -> list[StagingVerdict]:
        if limit is None or limit <= 0:
            return list(self._history)
        return list(self._history[-int(limit):])

    @property
    def quota(self) -> StagingQuota:
        return self._quota

    def status(self) -> dict[str, Any]:
        """A snapshot for the platform staging-status endpoint."""
        return {
            "quota": self._quota.snapshot(),
            "history_size": len(self._history),
            "recent": [v.to_json() for v in self._history[-10:]],
        }
