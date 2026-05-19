"""Neuron loader — enforce the contract at load time (docs/engineering/04).

The orchestrator can only reason about neurons if every neuron *actually*
satisfies the Protocol. Soma-evolved neurons are assumed potentially
malformed or malicious, so validation is structural and paranoid and happens
before a neuron is ever dispatched.

One constitutional invariant lives here too: **money flows through one neuron
only**. Any neuron that claims the ``treasury.write`` capability must be the
single canonical ``payments.facilitator``. This is the load-time guarantee
behind the treasury policy being enforced in exactly one place.
"""

from __future__ import annotations

import inspect

from ..neurons._base import CostEstimate, FailureMode, Neuron

TREASURY_WRITE_TAG = "treasury.write"
SOLE_TREASURY_NEURON = "payments.facilitator"

_REQUIRED_ASYNC_METHODS = ("plan", "execute", "escalate")


class NeuronContractError(Exception):
    """A neuron does not satisfy the contract. It is rejected; it never
    runs."""


def validate_neuron(obj: object) -> Neuron:
    """Return ``obj`` typed as :class:`Neuron` or raise
    :class:`NeuronContractError`. Pure; no side effects; no neuron code is
    executed during validation."""
    name = getattr(obj, "name", None)
    if not isinstance(name, str) or not name:
        raise NeuronContractError("neuron.name must be a non-empty str")
    version = getattr(obj, "version", None)
    if not isinstance(version, str) or not version:
        raise NeuronContractError(f"{name}: version must be a non-empty str")

    tags = getattr(obj, "capability_tags", None)
    if not isinstance(tags, frozenset) or not tags:
        raise NeuronContractError(
            f"{name}: capability_tags must be a non-empty frozenset"
        )

    if not isinstance(getattr(obj, "cost_estimate", None), CostEstimate):
        raise NeuronContractError(f"{name}: cost_estimate must be a CostEstimate")

    fms = getattr(obj, "failure_modes", None)
    if not isinstance(fms, list) or not all(isinstance(f, FailureMode) for f in fms):
        raise NeuronContractError(
            f"{name}: failure_modes must be a list[FailureMode]"
        )

    for meth in _REQUIRED_ASYNC_METHODS:
        fn = getattr(obj, meth, None)
        if fn is None or not inspect.iscoroutinefunction(fn):
            raise NeuronContractError(
                f"{name}: {meth} must be an async method"
            )

    # The one-money-door invariant.
    if TREASURY_WRITE_TAG in tags and name != SOLE_TREASURY_NEURON:
        raise NeuronContractError(
            f"{name}: only {SOLE_TREASURY_NEURON!r} may hold "
            f"{TREASURY_WRITE_TAG!r} — money flows through one neuron only"
        )

    if not isinstance(obj, Neuron):  # runtime_checkable structural check
        raise NeuronContractError(f"{name}: does not satisfy the Neuron Protocol")
    return obj  # type: ignore[return-value]


def load_catalog(neurons: list[object]) -> dict[str, Neuron]:
    """Validate every neuron and return a name→neuron catalog. Duplicate
    names are rejected (ambiguous routing). Exactly one treasury-write neuron
    must exist and it must be the canonical one."""
    catalog: dict[str, Neuron] = {}
    treasury_writers = 0
    for obj in neurons:
        n = validate_neuron(obj)
        if n.name in catalog:
            raise NeuronContractError(f"duplicate neuron name {n.name!r}")
        if TREASURY_WRITE_TAG in n.capability_tags:
            treasury_writers += 1
        catalog[n.name] = n
    if treasury_writers != 1:
        raise NeuronContractError(
            f"exactly one treasury-write neuron required, found {treasury_writers}"
        )
    return catalog
