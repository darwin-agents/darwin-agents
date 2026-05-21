"""Evolution: Phase-1 self-modification pipeline (docs/engineering/21).

:class:`PatchStagingService` stages a real soma patch in a sandbox
and runs the in-DNA harness against it. :class:`EvalRunner` resolves
the per-Darwin audit tail so the staging service replays real recent
situations, not just synthetic trigger tapes.

NOTE: not to be confused with :mod:`darwin_dna.evolution`, which is
the genome's reproduction + lesson distillation machinery. This
package is the platform-side staging coordinator the supervisor
installs on ``ctx.patch_staging``.
"""

from __future__ import annotations

from .eval_runner import EvalRequest, EvalRunner
from .patch_staging import (
    PatchStagingService,
    StagingQuota,
    StagingSandbox,
    StagingVerdict,
)

__all__ = [
    "EvalRequest",
    "EvalRunner",
    "PatchStagingService",
    "StagingQuota",
    "StagingSandbox",
    "StagingVerdict",
]
