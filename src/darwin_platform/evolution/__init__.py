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
from .soma_loader import (
    SomaLoader,
    SomaLoadResult,
    SomaValidationError,
)
from .soma_merge import (
    DiffApplyError,
    SomaForensicSnapshot,
    SomaMergeResult,
    SomaMerger,
    apply_unified_diff,
)

__all__ = [
    "DiffApplyError",
    "EvalRequest",
    "EvalRunner",
    "PatchStagingService",
    "SomaForensicSnapshot",
    "SomaLoadResult",
    "SomaLoader",
    "SomaMergeResult",
    "SomaMerger",
    "SomaValidationError",
    "StagingQuota",
    "StagingSandbox",
    "StagingVerdict",
    "apply_unified_diff",
]
