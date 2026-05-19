"""darwin_dna — Layer 0, the founding genome.

The signed, immutable floor below which no Darwin can fall. This package is
*read* by the running organism and *never written* by it (P1). Public surface
is intentionally tiny; the interesting contracts live in submodules.

Curated dependency allowlist only (docs/engineering/18): stdlib, pydantic,
cryptography, langgraph, litellm (client-only), httpx, structlog.
"""

from ._version import LAYER_0_VERSION

__all__ = ["LAYER_0_VERSION", "boot"]


def boot(*args, **kwargs):
    """Lazy re-export of :func:`darwin_dna.evolution.loop.boot`.

    Imported lazily so that merely importing the genome (e.g. for signature
    verification tooling) does not drag in the orchestrator graph.
    """
    from .evolution.loop import boot as _boot

    return _boot(*args, **kwargs)
