"""The machinery every Darwin needs to live. The hot paths live here."""

from .audit_logger import AuditError, AuditLogger, AuditSink, MerkleAnchorStore
from .eval_harness import (
    BehaviorDelta,
    EvalResult,
    FrozenScenario,
    evaluate_mutation_candidate,
)
from .fitness import FitnessScore, FitnessWindow, compute_fitness
from .kill_switch import KillSignal, KillSwitch, KillSwitchSource
from .ledger import Ledger, LedgerEntry, LedgerError
from .llm_router import LLMError, LLMRouter, LLMTransport, Tier, TierBudgetExceeded
from .neuron_loader import NeuronContractError, load_catalog, validate_neuron
from .patch_validator import FileChange, Patch, PatchDecision, validate_patch
from .treasury_policy import (
    PolicyDecision,
    StrategyEnvelope,
    TreasuryState,
    Verdict,
    evaluate,
)

__all__ = [
    "AuditError",
    "AuditLogger",
    "AuditSink",
    "MerkleAnchorStore",
    "BehaviorDelta",
    "EvalResult",
    "FrozenScenario",
    "evaluate_mutation_candidate",
    "FitnessScore",
    "FitnessWindow",
    "compute_fitness",
    "KillSignal",
    "KillSwitch",
    "KillSwitchSource",
    "Ledger",
    "LedgerEntry",
    "LedgerError",
    "LLMError",
    "LLMRouter",
    "LLMTransport",
    "Tier",
    "TierBudgetExceeded",
    "NeuronContractError",
    "load_catalog",
    "validate_neuron",
    "FileChange",
    "Patch",
    "PatchDecision",
    "validate_patch",
    "PolicyDecision",
    "StrategyEnvelope",
    "TreasuryState",
    "Verdict",
    "evaluate",
]
