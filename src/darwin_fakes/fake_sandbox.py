"""Fake E2B sandbox + fake browser substrate.

NOT part of the genome. The real system runs every neuron in a Firecracker
microVM and drives a Browser-Use session on Browserbase. For the simulation we
only need behaviour that is deterministic and that exercises the genome's
escalation path (P4): the browser occasionally hits a captcha wall.
"""

from __future__ import annotations


class FakeSandbox:
    """A no-op code-exec sandbox. Present for completeness — Layer 0's
    starter neurons do not execute arbitrary code."""

    def run_code(self, code: str) -> dict:
        return {"ok": True, "stdout": "", "stderr": ""}


class FakeBrowser:
    """Deterministic browser substrate consumed by browser.operator.

    Every Nth navigation hits a captcha so the simulation actually drives the
    escalate-don't-fail control flow rather than only the happy path.
    """

    def __init__(self, *, wallet=None, captcha_every: int = 7):
        self._wallet = wallet
        self._captcha_every = captcha_every
        self._runs = 0

    def run(self, steps: list[str]) -> dict:
        self._runs += 1
        cost = 0.01
        if self._wallet is not None:
            try:
                self._wallet.debit(cost, "browserbase:session")
            except Exception:
                return {"ok": False, "detail": "no funds for browser session"}

        if self._captcha_every and self._runs % self._captcha_every == 0:
            return {"blocked": "captcha", "detail": "hCaptcha on publish form",
                    "cost_usdc": cost}
        return {"ok": True, "cost_usdc": cost,
                "data": {"published": True, "url": "https://blog.example/post"}}
