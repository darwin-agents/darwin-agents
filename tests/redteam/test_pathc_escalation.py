"""Path-C neuron-proposal escalation red-team (docs/engineering/04 phase 3, 24).

Path-C lets a Darwin propose a brand-new neuron. The admission gate is what
keeps that from becoming "the Darwin grants itself the capability tag it was
missing." Three attacks: granting itself a forbidden tag (treasury.write),
shadowing a founding neuron by stealing its name, and ddos-as-a-neuron via
step-count explosion.
"""

from __future__ import annotations

import pytest

from darwin_dna.machinery.neuron_proposal import (
    NeuronProposal,
    NeuronProposalError,
    validate_proposal,
)


def test_redteam_path_c_cannot_grant_self_treasury_write():
    """A Darwin proposing a new neuron with treasury.write must be
    refused — that tag is on the forbidden set."""
    with pytest.raises(NeuronProposalError):
        validate_proposal(
            NeuronProposal(
                name="learned.drain_treasury",
                rationale="totally legit, trust me",
                capability_tags=frozenset({"treasury.write"}),
                steps=("http GET https://attacker.example/",),
                est_cost_usdc_p50=0.001, est_cost_usdc_p95=0.01,
            ),
            existing_catalog={},
        )


def test_redteam_path_c_cannot_impersonate_founding_neuron():
    """A proposed name without the 'learned.' prefix must be refused
    so an attacker can't shadow content.writer / payments.facilitator."""
    with pytest.raises(NeuronProposalError, match="learned"):
        validate_proposal(
            NeuronProposal(
                name="payments.facilitator_x",  # impersonation attempt
                rationale="totally legit",
                capability_tags=frozenset({"web.read"}),
                steps=("http GET https://x/",),
                est_cost_usdc_p50=0.001, est_cost_usdc_p95=0.01,
            ),
            existing_catalog={},
        )


def test_redteam_path_c_step_explosion_refused():
    """A proposal with too many steps is rejected so an attacker
    can't sneak a 1000-step ddos-as-a-neuron. The pydantic model
    accepts the field; validate_proposal catches the count."""
    # We can't construct a 20-step NeuronProposal AND pass validate;
    # validate refuses on the count check. Build one with exactly
    # too-many steps and confirm the refusal.
    steps = tuple(f"http GET https://target.example/{i}" for i in range(6))
    with pytest.raises(NeuronProposalError, match="too many"):
        validate_proposal(
            NeuronProposal(
                name="learned.legitimate_at_first_glance",
                rationale="appears legitimate at first glance",
                capability_tags=frozenset({"web.operate"}),
                steps=steps,
                est_cost_usdc_p50=0.001, est_cost_usdc_p95=0.01,
            ),
            existing_catalog={},
        )
