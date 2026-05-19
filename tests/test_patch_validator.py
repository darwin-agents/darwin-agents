"""Self-modification gate tests (docs/engineering/03)."""

from __future__ import annotations

import pytest

from darwin_dna.machinery.patch_validator import FileChange, Patch, validate_patch


def _p(path, diff="+    pass\n"):
    return Patch(rationale="r", changes=[FileChange(path=path, diff=diff)])


@pytest.mark.parametrize(
    "path",
    [
        "src/darwin_dna/hyperparameters.py",
        "darwin_dna/machinery/treasury_policy.py",
        "soma/orchestrator/nodes/patch_validator_helper.py",
        "../darwin_dna/constitution/spend_limits.py",
        "/etc/passwd",
        "soma/secrets/keys.py",  # not a writable subtree
    ],
)
def test_out_of_scope_paths_rejected(path):
    assert validate_patch(_p(path)).ok is False


@pytest.mark.parametrize(
    "path",
    [
        "soma/strategies/new_idea.py",
        "soma/orchestrator/nodes/plan.py",
        "soma/neurons/content_client.py",
        "soma/config.py",
    ],
)
def test_in_scope_paths_allowed(path):
    assert validate_patch(_p(path)).ok is True


def test_protected_anchor_in_scope_file_rejected():
    d = validate_patch(
        _p("soma/orchestrator/nodes/plan.py", diff="-    ctx.audit.log_intent('x', {})\n")
    )
    assert d.ok is False
    assert "protected anchor" in d.reason


def test_empty_patch_rejected():
    assert validate_patch(Patch(rationale="r", changes=[])).ok is False
