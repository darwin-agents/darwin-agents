"""Production evaluation runner (docs/engineering/21 §"Production eval runner").

Replays the last N real situations (drawn from the per-Darwin audit
sink) through the patched soma. The fitness delta returned by this
runner is what the supervisor consumes to decide promote vs reject.

This module is a thin orchestrator over :class:`PatchStagingService`:
its job is to (1) resolve the right audit sink for the Darwin,
(2) fetch the recent tail, and (3) hand it to the staging service.
Keeping it separate from staging lets future audit sources (e.g. a
Postgres event store) be wired in without changing the staging
contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from darwin_dna.machinery.eval_harness import BehaviorDelta
from darwin_dna.machinery.patch_validator import Patch

from .patch_staging import PatchStagingService, StagingVerdict


# AuditSinkResolver: given a darwin_id, return an object exposing
# ``read_all() -> list[dict]`` or ``None`` when the darwin is unknown.
AuditSinkResolver = Callable[[str], Any]


@dataclass(frozen=True)
class EvalRequest:
    """The four things the runner needs to evaluate one candidate."""

    darwin_id: str
    patch: Patch
    behavior_delta: BehaviorDelta
    failure_class: str
    recurrence: int


class EvalRunner:
    """Stage + evaluate a candidate patch against real recent audit entries.

    ``audit_sink_resolver`` is a callable ``darwin_id -> AuditSink | None``.
    The supervisor wires this to its per-Darwin sink lookup so the runner
    reads through the same durable storage the genome wrote to. Tests
    pass a lambda over an in-memory sink.
    """

    def __init__(
        self,
        *,
        staging: PatchStagingService,
        audit_sink_resolver: AuditSinkResolver,
        max_audit_entries: int = 200,
    ) -> None:
        self._staging = staging
        self._resolver = audit_sink_resolver
        self._max = max(1, int(max_audit_entries))

    def evaluate(self, req: EvalRequest) -> StagingVerdict:
        recent = self._collect_recent(req.darwin_id)
        return self._staging.stage_and_evaluate(
            darwin_id=req.darwin_id,
            patch=req.patch,
            behavior_delta=req.behavior_delta,
            failure_class=req.failure_class,
            recurrence=req.recurrence,
            recent_audit=recent,
        )

    def _collect_recent(self, darwin_id: str) -> list[dict[str, Any]]:
        sink = None
        try:
            sink = self._resolver(darwin_id)
        except Exception:
            sink = None
        if sink is None:
            return []
        try:
            rows = sink.read_all()
        except Exception:
            return []
        own = [r for r in rows if r.get("darwin_id") == darwin_id]
        return own[-self._max:]

    @property
    def staging(self) -> PatchStagingService:
        return self._staging
