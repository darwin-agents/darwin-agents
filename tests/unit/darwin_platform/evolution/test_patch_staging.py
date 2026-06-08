"""Tests for the Phase 1 patch-staging pipeline (docs/engineering/21).

Covers:
- Validator-rejected patches surface as ``promote=False`` (never reach
  the sandbox).
- In-process fallback (no sandbox factory) returns a verdict.
- Sandbox-backed path: stdout parsed, cost recorded, session id present.
- Sandbox stderr → ``promote=False`` with stderr in reason.
- Sandbox JSON parse error → ``promote=False`` with parse error.
- Quota cap: third concurrent call is rejected with a clear reason.
- Scenario synthesised from audit (real ``outcome`` records).
- Scenario falls back to ``from_trigger`` when audit is empty.
"""

from __future__ import annotations

import json


from darwin_dna.machinery.eval_harness import BehaviorDelta
from darwin_dna.machinery.patch_validator import FileChange, Patch
from darwin_platform.evolution.patch_staging import (
    PatchStagingService,
    StagingQuota,
)


def _valid_patch() -> Patch:
    return Patch(
        rationale="back off the flaky endpoint",
        changes=[FileChange(
            path="soma/strategies/content_affiliate.py",
            diff="+    pass\n",
        )],
    )


def _dna_patch() -> Patch:
    return Patch(
        rationale="sneak into DNA",
        changes=[FileChange(
            path="src/darwin_dna/hyperparameters.py",
            diff="+CAP=1e9\n",
        )],
    )


def _delta(after: int = 3) -> BehaviorDelta:
    return BehaviorDelta(
        kind="backoff", failure_class="flaky_io", after_failures=after,
    )


# ---------------------------------------------------------------------------
# Validator gate runs OUTSIDE the sandbox
# ---------------------------------------------------------------------------

def test_dna_patch_is_rejected_before_sandbox():
    """A patch that targets the genome never reaches the sandbox."""

    def factory():
        # The factory must NOT be called for a DNA-targeting patch.
        raise AssertionError("sandbox should not have been invoked")

    svc = PatchStagingService(sandbox_factory=factory)
    v = svc.stage_and_evaluate(
        darwin_id="d-1", patch=_dna_patch(), behavior_delta=_delta(),
        failure_class="flaky_io", recurrence=5,
    )
    assert v.promote is False
    assert "validator" in v.reason.lower()
    assert v.rejected_path == "src/darwin_dna/hyperparameters.py"
    assert v.sandbox_session_id == ""


# ---------------------------------------------------------------------------
# In-process fallback
# ---------------------------------------------------------------------------

_BASELINE_STRATEGY = (
    "from ._base import StrategyAction, StrategySpec\n"
    "\n"
    "\n"
    "class S:\n"
    "    _spec = StrategySpec(\n"
    '        id="base", archetype="content.affiliate",\n'
    '        neurons_used=frozenset({"content.writer"}),\n'
    "        required_capabilities=frozenset(),\n"
    '        fitness_signals=["x"], est_daily_cost_usdc=0.1,\n'
    "        revenue_cadence_days=7.0,\n"
    "    )\n"
    "\n"
    "    @property\n"
    "    def spec(self):\n"
    "        return self._spec\n"
    "\n"
    "    def propose(self, obs):\n"
    '        return StrategyAction(kind="wait", reason="idle")\n'
    "\n"
    "\n"
    "def founding_strategy():\n"
    "    return S()\n"
)

# Flip the idle baseline into an active (metabolising) candidate.
_STRATEGY_DIFF = (
    "--- a/soma/strategies/content_affiliate.py\n"
    "+++ b/soma/strategies/content_affiliate.py\n"
    "@@ -18,1 +18,1 @@\n"
    '-        return StrategyAction(kind="wait", reason="idle")\n'
    '+        return StrategyAction(kind="dispatch", reason="go", '
    'neuron="content.writer")\n'
)


def test_real_soma_strategy_eval_runs_the_candidate(tmp_path):
    """With a soma root, a strategy-rewriting patch is scored by RUNNING the
    real candidate (doc 33) — an active rewrite of an idle baseline promotes,
    and the verdict is stamped from the real-soma path, not the proxy."""
    live = tmp_path / "d-soma" / "soma" / "strategies"
    live.mkdir(parents=True)
    (live / "content_affiliate.py").write_text(_BASELINE_STRATEGY)

    svc = PatchStagingService(sandbox_factory=None, soma_root=str(tmp_path))
    patch = Patch(
        rationale="metabolise instead of idling",
        changes=[FileChange(
            path="soma/strategies/content_affiliate.py", diff=_STRATEGY_DIFF,
        )],
    )
    v = svc.stage_and_evaluate(
        darwin_id="d-soma", patch=patch, behavior_delta=_delta(after=2),
        failure_class="flaky_io", recurrence=5,
    )
    assert v.sandbox_session_id == "real-soma-d-soma"  # real path, not proxy
    assert v.promote is True
    assert v.candidate_score > v.baseline_score


def test_real_soma_eval_falls_through_when_patch_wont_apply(tmp_path):
    """If the candidate hunk can't apply (no matching base), the real-soma path
    returns None and the proxy BehaviorDelta path still produces a verdict."""
    svc = PatchStagingService(sandbox_factory=None, soma_root=str(tmp_path))
    # A hunk that won't match any base file (no live soma, no founding line).
    patch = Patch(
        rationale="bad hunk",
        changes=[FileChange(
            path="soma/strategies/content_affiliate.py",
            diff="@@ -999,1 +999,1 @@\n-NOPE\n+NOPED\n",
        )],
    )
    v = svc.stage_and_evaluate(
        darwin_id="d-x", patch=patch, behavior_delta=_delta(after=2),
        failure_class="flaky_io", recurrence=10,
    )
    # Fell through to the proxy harness (in-process), still a verdict.
    assert v.sandbox_session_id.startswith("in_process-")


def test_in_process_fallback_returns_verdict():
    svc = PatchStagingService(sandbox_factory=None)
    v = svc.stage_and_evaluate(
        darwin_id="d-1", patch=_valid_patch(),
        behavior_delta=_delta(after=2),
        failure_class="flaky_io", recurrence=10,
    )
    # The Layer-0 harness must judge a recurrence=10 / after=2 backoff a
    # clean improvement.
    assert v.promote is True
    assert v.candidate_score > v.baseline_score
    assert v.sandbox_cost_usdc == 0.0
    assert v.sandbox_session_id.startswith("in_process-")
    assert v.ran_in_sandbox is False


def test_in_process_fallback_archives_when_no_improvement():
    svc = PatchStagingService(sandbox_factory=None)
    v = svc.stage_and_evaluate(
        darwin_id="d-1", patch=_valid_patch(),
        behavior_delta=_delta(after=5),  # never triggers at recurrence=2
        failure_class="flaky_io", recurrence=2,
    )
    assert v.promote is False
    assert v.candidate_score <= v.baseline_score


# ---------------------------------------------------------------------------
# Sandbox-backed happy path
# ---------------------------------------------------------------------------

class _StubSandbox:
    """Captures the script + returns a deterministic verdict."""

    def __init__(self, *, ok: bool = True, stdout: str = "", stderr: str = ""):
        self.calls: list[dict] = []
        self._ok = ok
        self._stdout = stdout
        self._stderr = stderr

    def run_code(self, *, language, code, timeout_seconds=30.0):
        self.calls.append({"language": language, "code": code,
                           "timeout_seconds": timeout_seconds})
        return {
            "ok": self._ok,
            "exit_code": 0 if self._ok else 1,
            "stdout": self._stdout,
            "stderr": self._stderr,
            "cost_usdc": 0.02,
        }


def test_sandbox_happy_path_returns_parsed_verdict():
    stub = _StubSandbox(ok=True, stdout=json.dumps({
        "promote": True, "reason": "improved",
        "baseline_score": 0.10, "candidate_score": 0.30,
    }))
    svc = PatchStagingService(sandbox_factory=lambda: stub)
    v = svc.stage_and_evaluate(
        darwin_id="d-1", patch=_valid_patch(),
        behavior_delta=_delta(), failure_class="flaky_io", recurrence=5,
    )
    assert v.promote is True
    assert v.baseline_score == 0.10
    assert v.candidate_score == 0.30
    assert v.sandbox_cost_usdc == 0.02
    assert v.sandbox_session_id.startswith("staging-d-1-")
    assert v.ran_in_sandbox is True
    # The script must reference the eval harness so a real sandbox can run it.
    assert "evaluate_mutation_candidate" in stub.calls[0]["code"]
    assert "BehaviorDelta" in stub.calls[0]["code"]


def test_sandbox_failure_surfaces_stderr_in_reason():
    stub = _StubSandbox(ok=False, stdout="", stderr="ImportError: boom")
    svc = PatchStagingService(sandbox_factory=lambda: stub)
    v = svc.stage_and_evaluate(
        darwin_id="d-1", patch=_valid_patch(),
        behavior_delta=_delta(), failure_class="flaky_io", recurrence=5,
    )
    assert v.promote is False
    assert "ImportError" in v.reason


def test_sandbox_unparseable_stdout_is_rejected():
    stub = _StubSandbox(ok=True, stdout="not valid json")
    svc = PatchStagingService(sandbox_factory=lambda: stub)
    v = svc.stage_and_evaluate(
        darwin_id="d-1", patch=_valid_patch(),
        behavior_delta=_delta(), failure_class="flaky_io", recurrence=5,
    )
    assert v.promote is False
    assert "unparseable" in v.reason.lower()


def test_sandbox_factory_raises_returns_error_verdict():
    def factory():
        raise RuntimeError("e2b api key missing")

    svc = PatchStagingService(sandbox_factory=factory)
    v = svc.stage_and_evaluate(
        darwin_id="d-1", patch=_valid_patch(),
        behavior_delta=_delta(), failure_class="flaky_io", recurrence=5,
    )
    assert v.promote is False
    assert "sandbox init failed" in v.reason


# ---------------------------------------------------------------------------
# Quota enforcement
# ---------------------------------------------------------------------------

def test_quota_exhausted_rejects_with_clear_reason():
    quota = StagingQuota(max_concurrent=1)
    # Pre-acquire the only slot so the service's call exhausts it.
    assert quota.acquire() is True
    stub = _StubSandbox(ok=True, stdout=json.dumps({
        "promote": True, "reason": "x",
        "baseline_score": 0.0, "candidate_score": 1.0,
    }))
    svc = PatchStagingService(sandbox_factory=lambda: stub, quota=quota)
    v = svc.stage_and_evaluate(
        darwin_id="d-1", patch=_valid_patch(),
        behavior_delta=_delta(), failure_class="flaky_io", recurrence=5,
    )
    assert v.promote is False
    assert "quota exhausted" in v.reason
    assert stub.calls == []  # never reached the sandbox
    quota.release()
    snap = quota.snapshot()
    assert snap["total_rejected"] >= 1


# ---------------------------------------------------------------------------
# Scenario synthesis from audit
# ---------------------------------------------------------------------------

def _outcome(status: str, *, failure_class: str | None = None,
             error_code: str | None = None):
    detail: dict = {}
    if failure_class is not None:
        detail["failure_class"] = failure_class
    if error_code is not None:
        detail["error_code"] = error_code
    return {
        "kind": "outcome",
        "darwin_id": "d-1",
        "payload": {"intent_id": "x", "status": status, "detail": detail},
    }


def test_scenario_from_audit_uses_real_outcomes():
    svc = PatchStagingService(sandbox_factory=None)
    audit = [
        _outcome("ok"),
        _outcome("fail", error_code="flaky_io"),
        _outcome("ok"),
        _outcome("fail", error_code="other"),  # different class — ignored
        _outcome("fail", failure_class="flaky_io"),
        _outcome("ok"),
    ]
    # Use a low-improvement delta so the math is easy to follow.
    v = svc.stage_and_evaluate(
        darwin_id="d-1", patch=_valid_patch(),
        behavior_delta=BehaviorDelta(
            kind="backoff", failure_class="flaky_io", after_failures=1,
        ),
        failure_class="flaky_io", recurrence=1,
        recent_audit=audit,
    )
    # With the synthesised tape (3 oks + 2 fails) and backoff after 1 fail,
    # candidate avoids the second fail → strict improvement → promote=True.
    assert v.promote is True
    assert v.candidate_score > v.baseline_score


def test_scenario_falls_back_to_trigger_when_audit_empty():
    svc = PatchStagingService(sandbox_factory=None)
    v = svc.stage_and_evaluate(
        darwin_id="d-1", patch=_valid_patch(),
        behavior_delta=_delta(after=2), failure_class="flaky_io",
        recurrence=8, recent_audit=[],
    )
    # FrozenScenario.from_trigger("flaky_io", 8) interleaves 8 fails + 20 oks;
    # backoff after 2 should clearly win.
    assert v.promote is True


# ---------------------------------------------------------------------------
# History + status snapshot
# ---------------------------------------------------------------------------

def test_history_records_each_verdict():
    svc = PatchStagingService(sandbox_factory=None, history_cap=4)
    for _ in range(6):
        svc.stage_and_evaluate(
            darwin_id="d-1", patch=_valid_patch(),
            behavior_delta=_delta(after=2),
            failure_class="flaky_io", recurrence=5,
        )
    hist = svc.history()
    assert len(hist) == 4  # capped
    snap = svc.status()
    assert "quota" in snap
    assert "history_size" in snap
    assert snap["history_size"] == 4
    assert isinstance(snap["recent"], list)
