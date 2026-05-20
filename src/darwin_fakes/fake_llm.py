"""Deterministic fake LiteLLM gateway (LLMTransport).

NOT part of the genome. Returns heuristic, reproducible responses keyed by the
capability tier and the task — no network, no randomness, so a 90-day
simulation is bit-for-bit repeatable. Bills the wallet on every call, the way
the real platform gateway meters a Darwin's virtual key, so LLM spend is real
runway pressure.
"""

from __future__ import annotations

import hashlib

# Per-call cost by tier (USD). Coding is deliberately ~50x reasoning so the
# earn-before-mutate economics actually bite in the sim.
_TIER_COST = {
    "fast": 0.0008,
    "reasoning": 0.004,
    "coding": 0.20,
    "image_gen": 0.02,
    "embedding": 0.0001,
}

_LOREM = (
    "Choosing well here comes down to three things: fit, durability, and the "
    "cost of being wrong. Most buyers over-index on the first and ignore the "
    "third. Below we walk the trade-offs and link the picks we actually trust."
)


class FakeLLM:
    def __init__(self, wallet=None):
        self._wallet = wallet
        self.calls: list[tuple[str, str]] = []

    def _bill(self, tier: str) -> float:
        cost = _TIER_COST.get(tier, 0.001)
        if self._wallet is not None:
            try:
                self._wallet.debit(cost, f"llm:{tier}")
            except Exception:
                # Out of money mid-call: report zero-cost empty completion.
                # The genome treats this as a degraded tier and backs off.
                return 0.0
        return cost

    def complete(self, tier: str, request: dict) -> dict:
        task = request.get("task", "")
        self.calls.append((tier, task))
        cost = self._bill(tier)

        if task == "write_long_form":
            target = int(request.get("target_words", 800))
            topic = str(request.get("topic", "the topic"))
            body = (f"# {topic.title()}\n\n" + (_LOREM + " ") * (target // 40 + 1))
            words = len(body.split())
            # higher tiers write better; reasoning is the default writer tier
            quality = 0.72 if tier == "reasoning" else 0.55
            return {"text": body, "words": words, "quality_score": quality,
                    "cost_usdc": cost}

        if task == "propose_patch":
            fc = str(request.get("failure_class", "unknown"))
            return {
                "rationale": f"address recurring {fc} by backing off the failing path",
                "changes": [
                    {
                        "path": "soma/strategies/content_affiliate.py",
                        "diff": f"+    # {fc}: take the cheaper fallback route\n",
                    }
                ],
                # The executable form the harness actually runs (the diff
                # above travels only for audit/legibility).
                "behavior_delta": {
                    "kind": "backoff",
                    "failure_class": fc,
                    "after_failures": 2,
                },
                # Path-C (doc 04 phase 3): FakeLLM never emits a neuron
                # proposal by default — the 90-day sim shouldn't grow
                # the catalog from coding-tier hallucinations. Tests
                # that exercise the Path-C path stub the transport
                # directly instead.
                "neuron_proposal": None,
                "cost_usdc": cost,
            }

        if task == "plan":
            # Advisory only; the genome does not depend on parsing this.
            h = hashlib.sha256(str(request).encode()).hexdigest()[:6]
            return {"text": f"proceed per strategy prior ({h})", "cost_usdc": cost}

        if task == "research_synthesis":
            query = str(request.get("query", ""))
            results = list(request.get("results", []))
            return {
                "text": (
                    f"Summary of {len(results)} result(s) for {query!r}: "
                    + (results[0].get("title", "no title") if results else "no findings")
                ),
                "cost_usdc": cost,
            }

        if task == "support_reply":
            incoming = str(request.get("incoming_message", ""))
            channel = str(request.get("channel", ""))
            return {
                "text": (
                    f"Thanks for reaching out on {channel}. "
                    f"We have received your message and will address it "
                    f"shortly. (Re: {incoming[:80]})"
                ),
                "confidence": 0.75,
                "cost_usdc": cost,
            }

        if task == "analytics_synthesis":
            scope = str(request.get("scope", ""))
            metrics = dict(request.get("raw_metrics", {}))
            # Synthetic ROI: revenue minus cost over cost, or 0 if either is
            # missing. Deterministic so the sim stays bit-for-bit reproducible.
            rev = float(metrics.get("revenue_usdc", 0.0))
            cst = float(metrics.get("cost_usdc", 0.0))
            roi = (rev - cst) / cst if cst > 0 else 0.0
            return {
                "text": f"{scope}: revenue=${rev:.2f}, cost=${cst:.2f}, roi={roi:.2f}",
                "roi": roi,
                "cost_usdc": cost,
            }

        return {"text": "", "cost_usdc": cost}
