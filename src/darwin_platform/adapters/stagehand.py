"""Stagehand-style cached replay substrate (docs/engineering/15).

Doc 15: once Browser-Use has succeeded on the same task signature ≥N
times, the platform promotes the trace to a *cached replay* path.
Stagehand is the production-shaped library for that — deterministic,
LLM-free replay of a recorded action sequence. This module ships the
adapter that conforms to the same ``BrowserClient`` Protocol so
:class:`CachedBrowserSubstrate` plugs it in as the cheap path.

Layer 0 wires this with a deterministic stub backend (matching the
existing FakeBrowser shape) and exposes the same SDK-injected pattern
the other adapters use. Production deployers pass a real Stagehand
client built via their ``stagehand_factory``.

Replay is **byte-for-byte deterministic** — same recorded sequence
always produces the same outcome shape. That's the point: cached
replays should not consume LLM budget and should not surprise
operators with fresh exploration costs."""

from __future__ import annotations

from typing import Any, Callable


class StagehandError(Exception):
    """Replay refused or stagehand client raised."""


class StagehandReplayClient:
    """Drop-in :class:`BrowserClient` that replays a recorded action
    sequence via the injected ``stagehand_factory``.

    Construction takes:
      - ``wallet``: WalletPort. Billed per replay (cheap; default
        $0.001 vs $0.05+ for an LLM-driven Browser-Use session).
      - ``stagehand_factory``: zero-arg callable returning a client
        with a ``run(steps) -> dict`` method. Tests inject a stub;
        production wires the stagehand SDK client.
      - ``cost_per_replay``: USD billed per call. Default $0.001 —
        ~100x cheaper than Browser-Use exploration. Calibrate per
        production deploy.

    The Protocol-conforming ``run(steps) -> dict`` return shape:
      - ``{"ok": True, "data": {...}, "cost_usdc": float, "via": "stagehand_replay"}``
      - ``{"ok": False, "detail": str, "cost_usdc": float, "via": "stagehand_replay"}``

    Stagehand cannot solve captchas — a captcha-like outcome surfaces
    as a navigation_failed (``ok=False``) so the cached substrate's
    demotion machinery can demote the cache and the next exploration
    attempt picks up.
    """

    name = "stagehand_replay"
    # Marker so the cache substrate can probe whether to nest this as
    # the "replay" half vs the exploration half.
    is_replay = True

    def __init__(
        self,
        *,
        wallet: Any,
        stagehand_factory: Callable[[], Any],
        cost_per_replay: float = 0.001,
    ) -> None:
        if stagehand_factory is None:
            raise ValueError("StagehandReplayClient needs a stagehand_factory")
        self._wallet = wallet
        self._factory = stagehand_factory
        self._cost = float(cost_per_replay)

    # -- BrowserClient -----------------------------------------------------
    def run(self, steps: list[str]) -> dict:
        # Bill first (microscopic cost; refusal here means the wallet
        # is broken).
        try:
            self._wallet.debit(self._cost, "stagehand:replay")
        except Exception as exc:
            return {
                "ok": False, "detail": f"wallet refused: {exc}",
                "cost_usdc": 0.0, "via": "stagehand_replay",
            }
        try:
            client = self._factory()
        except Exception as exc:
            return {
                "ok": False, "detail": f"stagehand init failed: {exc}",
                "cost_usdc": self._cost, "via": "stagehand_replay",
            }
        try:
            outcome = client.run(list(steps)) if hasattr(client, "run") else client(list(steps))
        except Exception as exc:
            return {
                "ok": False, "detail": f"stagehand replay failed: {exc}",
                "cost_usdc": self._cost, "via": "stagehand_replay",
            }
        # Normalise the response shape. Stagehand returns
        # {"success": bool, "data"?: {...}, "error"?: str} OR a raw dict;
        # we map both to the BrowserClient shape.
        if isinstance(outcome, dict):
            if "ok" in outcome:
                # Already in our shape.
                result = dict(outcome)
            elif outcome.get("success") is True:
                result = {"ok": True, "data": dict(outcome.get("data") or {})}
            elif outcome.get("success") is False:
                result = {"ok": False, "detail": str(outcome.get("error") or "replay failed")}
            else:
                result = {"ok": True, "data": dict(outcome)}
        else:
            result = {"ok": True, "data": {"text": str(outcome)}}
        result.setdefault("cost_usdc", self._cost)
        result["via"] = "stagehand_replay"
        # Stagehand can't solve captchas; surface as nav_failed so the
        # cache substrate demotes and the next exploration tries.
        detail = str(result.get("detail", "")).lower()
        if not result.get("ok") and ("captcha" in detail or "human" in detail):
            # Keep "blocked"=captcha so the cached substrate's
            # captcha-not-eligible-for-vision branch fires the same
            # way it does for the exploration path.
            result.setdefault("blocked", "captcha")
        return result


class InMemoryStagehandClient:
    """Deterministic stub for tests + Layer 0 development. Replays a
    pre-loaded outcome per call. Matches Stagehand's
    ``run(steps) -> {success, data?, error?}`` shape."""

    def __init__(self, outcomes: list[dict]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[list[str]] = []

    def run(self, steps: list[str]) -> dict:
        self.calls.append(list(steps))
        if not self._outcomes:
            return {"success": True, "data": {}}
        return self._outcomes.pop(0)
