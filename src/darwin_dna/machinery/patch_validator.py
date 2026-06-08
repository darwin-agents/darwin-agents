"""Patch validator — the self-modification gate (docs/engineering/03).

A soma rewrite is staged in a fresh sandbox, but it must clear this validator
*first*. The validator is in the DNA precisely so a Darwin cannot rewrite the
thing that judges its rewrites. It is pure and conservative: when in doubt,
reject. A rejected patch is archived as a negative lesson, never applied.

In scope (the agent may rewrite): soma strategies, soma neuron *clients*, most
orchestrator nodes, soma hyperparameter config.
Out of scope (always rejected): anything under ``darwin_dna/``, the patch
validator itself, and any hunk that touches a protected anchor — the
kill-switch poll, the audit-logger calls, the treasury enforcement points.
"""

from __future__ import annotations

import posixpath

from pydantic import BaseModel


class FileChange(BaseModel):
    model_config = {"frozen": True}

    path: str  # repo-relative, POSIX
    diff: str  # unified-diff text (added '+' / removed '-' lines inspected)


class Patch(BaseModel):
    model_config = {"frozen": True}

    rationale: str
    changes: list[FileChange]


class PatchDecision(BaseModel):
    model_config = {"frozen": True}

    ok: bool
    rejected_path: str | None = None
    reason: str | None = None


# Hard out-of-scope path markers. The DNA package is sacrosanct (P1).
_DNA_PREFIXES = ("darwin_dna/", "src/darwin_dna/")
_VALIDATOR_MARKERS = ("patch_validator", "kill_switch", "audit_logger", "treasury_policy")

# The writable soma subtrees. ``soma/neurons/`` is included so a Darwin can
# author a whole neuron via the same validated merge path as a strategy (doc
# 33) — the WRITE is gated here (no DNA, no enforcement modules, no protected
# anchors); the RUN is gated independently by ``SomaLoader.load_neurons`` (the
# Neuron protocol + the Path-C capability-tag allowlist + the one-money-door
# contract check), so a self-authored neuron is held to the same envelope as a
# Path-C structured proposal.
_IN_SCOPE_PREFIXES = (
    "soma/strategies/",
    "soma/orchestrator/nodes/",
    "soma/neurons/",
)
_IN_SCOPE_EXACT = ("soma/config.py",)


def _is_neuron_client(path: str) -> bool:
    # Subsumed by the ``soma/neurons/`` prefix above; kept for callers/tests
    # that probe the neuron-client path explicitly.
    return path.startswith("soma/neurons/") and path.endswith("_client.py")


# Anchors that must never be introduced or deleted by a patch to an in-scope
# file. These are the enforcement points an adversarial soma would target.
_PROTECTED_ANCHORS = (
    "kill_switch.check(",
    ".log_intent(",
    ".log_outcome(",
    "treasury_policy.evaluate(",
    "evaluate(",  # treasury policy entrypoint, even if imported bare
)


def _changed_lines(diff: str) -> list[str]:
    """The added/removed payload lines of a unified diff (ignoring the
    +++/--- file headers and @@ hunk headers)."""
    out = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---", "@@")):
            continue
        if line and line[0] in "+-":
            out.append(line[1:])
    return out


def validate_patch(patch: Patch) -> PatchDecision:
    """Pure. Returns the first rejection or an approval."""
    if not patch.changes:
        return PatchDecision(ok=False, reason="empty patch")

    for change in patch.changes:
        path = posixpath.normpath(change.path).lstrip("./")

        # Path traversal or absolute escape — refuse outright.
        if path.startswith("/") or ".." in path.split("/"):
            return PatchDecision(
                ok=False, rejected_path=change.path,
                reason="path escapes the soma sandbox",
            )

        # The DNA is read-only, forever (P1).
        if any(path.startswith(p) or p.rstrip("/") in path for p in _DNA_PREFIXES):
            return PatchDecision(
                ok=False, rejected_path=change.path,
                reason="patch targets the immutable DNA core",
            )

        # The validator and the other in-DNA enforcement modules by name.
        if any(m in path for m in _VALIDATOR_MARKERS):
            return PatchDecision(
                ok=False, rejected_path=change.path,
                reason="patch targets a protected enforcement module",
            )

        # Must land in an explicitly writable location.
        in_scope = (
            path in _IN_SCOPE_EXACT
            or path.startswith(_IN_SCOPE_PREFIXES)
            or _is_neuron_client(path)
        )
        if not in_scope:
            return PatchDecision(
                ok=False, rejected_path=change.path,
                reason=f"{path} is not in a soma-writable subtree",
            )

        # Even in-scope, a hunk may not add or remove a protected anchor:
        # the kill-switch poll, the audit calls, the treasury enforcement.
        for line in _changed_lines(change.diff):
            stripped = line.strip()
            if any(anchor in stripped for anchor in _PROTECTED_ANCHORS):
                return PatchDecision(
                    ok=False, rejected_path=change.path,
                    reason=(
                        "patch alters a protected anchor (kill-switch / audit "
                        "/ treasury enforcement)"
                    ),
                )

    return PatchDecision(ok=True)
