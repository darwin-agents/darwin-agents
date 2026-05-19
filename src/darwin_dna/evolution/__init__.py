"""The evolution machinery: the loop, reproduction, distillation, archive."""

from .archive import ArchiveEntry, MutationArchive
from .lesson_distiller import DistillationError, distill
from .loop import DarwinRuntime, EscalationInbox, Platform, boot
from .reproduction import (
    ChildSpawner,
    PlatformSigner,
    ReproductionResult,
    reproduce,
)

__all__ = [
    "ArchiveEntry",
    "MutationArchive",
    "DistillationError",
    "distill",
    "DarwinRuntime",
    "EscalationInbox",
    "Platform",
    "boot",
    "ChildSpawner",
    "PlatformSigner",
    "ReproductionResult",
    "reproduce",
]
