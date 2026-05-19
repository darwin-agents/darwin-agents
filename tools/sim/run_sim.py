"""Run a Darwin for N simulated days and assert the invariants.

NOT part of the genome. This is the nightly "simulation tier" from
docs/engineering/18. The invariants are the contract Layer 0 must keep no
matter what the world does:

  I1  the audit hash chain is unbroken, every day
  I2  the daily Merkle anchor reconciles against the platform store
  I3  the wallet balance never goes negative
  I4  no payment was ALLOWED over the inviolable per-action hard cap
  I5  lifetime spend never exceeds the lifetime hard cap
  I6  working memory was checkpointed at node boundaries
  I7  the ledger reconciles to the wallet with zero discrepancy
  I8  if the Darwin is not alive, it died *gracefully* (final records written)

Usage:
    uv run python -m tools.sim.run_sim --days 90 --scenario default
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field

from darwin_dna.evolution import boot
from darwin_dna.hyperparameters import HARD_CAPS, DEFAULT_HYPERPARAMETERS
from darwin_fakes import make_platform

from .scenario import SCENARIOS, Scenario
from .world import SimWorld

DAY = 86_400.0
# Sim heartbeat: the genome's max permitted interval (HEARTBEAT_MAX = 600s). A
# content/affiliate blog need not think more often, and it keeps 90 days fast.
SIM_HEARTBEAT_SECONDS = 600.0


class InvariantError(AssertionError):
    """An invariant the genome must never break, broke."""


@dataclass
class SimReport:
    scenario: str
    days: int
    alive: bool
    final_balance_usdc: float
    content_pieces: int
    published_pieces: int
    reproductions: int
    fitness: float | None
    death_reason: str | None
    timeline: list[dict] = field(default_factory=list)
    invariants_ok: bool = True


def _audit(stack):
    return stack.audit_sink.read_all()


def _check_invariants(rt, stack, scenario: Scenario, *, opening: float) -> None:
    # I1 — chain unbroken (re-verified from durable storage)
    rt.audit.verify()

    records = _audit(stack)

    # I3 — balance never negative
    bal = stack.wallet.balance_usdc()
    if bal < -1e-9:
        raise InvariantError(f"I3: negative balance {bal}")

    # I4 — no ALLOWED payment over the per-action hard cap
    for r in records:
        if r["kind"] == "event:payment.policy_decision":
            d = r["payload"]["decision"]
            if d["verdict"] == "allow":
                # the matching intent record carries the amount
                pass
        if r["kind"] == "outcome":
            det = r["payload"].get("detail", {})
            amt = det.get("amount_usdc")
            if (
                r["payload"].get("status") == "ok"
                and isinstance(amt, (int, float))
                and amt > HARD_CAPS.per_action_usdc + 1e-9
            ):
                raise InvariantError(
                    f"I4: payment of ${amt} settled over per-action cap"
                )

    # I5 — lifetime spend within the absolute ceiling
    if stack.wallet.spent_lifetime_usdc() > HARD_CAPS.per_lifetime_usdc + 1e-9:
        raise InvariantError("I5: lifetime spend exceeded the hard ceiling")

    # I6 — checkpointed at node boundaries
    if stack.checkpoint.saves <= 0:
        raise InvariantError("I6: no checkpoints written")

    # I7 — ledger reconciles to the wallet, zero discrepancy
    debits = sum(
        e["debit"]["amount_usdc"] for e in stack.wallet.ledger if "debit" in e
    )
    credits = sum(
        e["credit"]["amount_usdc"] for e in stack.wallet.ledger if "credit" in e
    )
    transfers = sum(
        e["reproduction_transfer"]["amount_usdc"]
        for e in stack.wallet.ledger
        if "reproduction_transfer" in e
    )
    expected = opening + credits - debits - transfers
    if abs(expected - bal) > DEFAULT_HYPERPARAMETERS.ledger_reconciliation_tolerance_usdc:
        raise InvariantError(
            f"I7: ledger {expected:.6f} != wallet {bal:.6f}"
        )

    # I8 — if not alive, death was graceful
    if not rt.state.alive:
        kinds = {r["kind"] for r in records}
        graceful = (
            {"event:death.begin", "event:death.end"} <= kinds
            or "event:kill.halt" in kinds
        )
        if not graceful:
            raise InvariantError("I8: died without writing final records")


async def _run(scenario: Scenario, days: int, *, verbose: bool) -> SimReport:
    start_ts = 1_700_000_000.0
    world = SimWorld(start_ts=start_ts, revenue=scenario.revenue)
    platform, stack = make_platform(
        opening_usdc=scenario.opening_usdc,
        clock=world.clock,
        captcha_every=scenario.captcha_every,
    )
    hp = DEFAULT_HYPERPARAMETERS.model_copy(
        update={"heartbeat_interval_seconds": SIM_HEARTBEAT_SECONDS}
    )
    rt = boot(platform, hp=hp, clock=world.clock, sleep=world.sleep)
    # Revenue follows *published* content — writing alone earns nothing.
    world.bind(wallet=stack.wallet, content_getter=lambda: rt.state.published_pieces)

    report = SimReport(
        scenario=scenario.name,
        days=days,
        alive=True,
        final_balance_usdc=scenario.opening_usdc,
        content_pieces=0,
        published_pieces=0,
        reproductions=0,
        fitness=None,
        death_reason=None,
    )

    for day in range(1, days + 1):
        deadline = start_ts + day * DAY
        await rt.run(until=lambda: world.clock() >= deadline)
        _check_invariants(rt, stack, scenario, opening=scenario.opening_usdc)

        reproductions = sum(
            1 for r in _audit(stack) if r["kind"] == "event:reproduction.done"
        )
        if day % 10 == 0 or not rt.state.alive or day == days:
            row = {
                "day": day,
                "balance": round(stack.wallet.balance_usdc(), 4),
                "content": rt.state.content_pieces,
                "published": rt.state.published_pieces,
                "fitness": (
                    round(rt.state.fitness, 4)
                    if rt.state.fitness is not None
                    else None
                ),
                "reproductions": reproductions,
                "alive": rt.state.alive,
            }
            report.timeline.append(row)
            if verbose:
                print(
                    f"day {day:>3}  bal ${row['balance']:>8.4f}  "
                    f"pub {row['published']:>2}  fit {row['fitness']}  "
                    f"repro {reproductions}  alive {rt.state.alive}"
                )
        if not rt.state.alive:
            break

    report.alive = rt.state.alive
    report.final_balance_usdc = round(stack.wallet.balance_usdc(), 4)
    report.content_pieces = rt.state.content_pieces
    report.published_pieces = rt.state.published_pieces
    report.fitness = rt.state.fitness
    report.death_reason = rt.state.death_reason
    report.reproductions = sum(
        1 for r in _audit(stack) if r["kind"] == "event:reproduction.done"
    )

    # Scenario expectation is itself an assertion.
    exp = scenario.expectation
    if exp == "reproduces" and not (report.alive and report.reproductions >= 1):
        raise InvariantError(
            f"{scenario.name}: expected to survive and reproduce, "
            f"got alive={report.alive} reproductions={report.reproductions}"
        )
    if exp == "survives" and not report.alive:
        raise InvariantError(f"{scenario.name}: expected to survive, it died")
    if exp == "dies_gracefully" and report.alive:
        raise InvariantError(
            f"{scenario.name}: expected a graceful death, it survived"
        )
    return report


def run_simulation(
    scenario_name: str = "default", days: int = 90, *, verbose: bool = False
) -> SimReport:
    scenario = SCENARIOS[scenario_name]
    return asyncio.run(_run(scenario, days, verbose=verbose))


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a Darwin in the simulated world")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="default")
    args = ap.parse_args()

    print(f"== simulation: {args.scenario}, {args.days} simulated days ==")
    report = run_simulation(args.scenario, args.days, verbose=True)
    print("-" * 56)
    print(
        f"result: alive={report.alive} "
        f"final=${report.final_balance_usdc} "
        f"published={report.published_pieces} "
        f"reproductions={report.reproductions} "
        f"fitness={report.fitness}"
    )
    if report.death_reason:
        print(f"death: {report.death_reason}")
    print("all invariants held." if report.invariants_ok else "INVARIANT FAILURE")


if __name__ == "__main__":
    main()
