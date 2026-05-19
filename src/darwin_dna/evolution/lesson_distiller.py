"""Distill runtime memory into a candidate DNA layer (docs/engineering/06, 02).

At a reproduction event the lineage's lived experience is compressed into a
small set of *legible, structured* lessons that the child inherits forever.
Code never goes in a layer — only data + pure-functional hints.

A coding/distiller-tier model does the prose in production (system prompt:
``prompts/distiller_system.txt``); the genome ships a deterministic fallback
so reproduction works on first boot even with a degraded model. Either way the
output is validated for legibility and constitution-safety before it can be
sealed.
"""

from __future__ import annotations

from ..layers import Lesson


class DistillationError(Exception):
    """The candidate layer is not safe or not legible. Reproduction aborts;
    the parent keeps running (a failed distillation is not fatal)."""


def distill(
    *,
    generation: int,
    recurring_failures: dict[str, int],
    dead_ends: set[str],
    successful_strategy_id: str | None,
    profit_usdc: float,
) -> list[Lesson]:
    """Produce a small, high-confidence lesson set from what actually
    happened. Deterministic; conservative; legible."""
    lessons: list[Lesson] = []

    # Negative lessons: failure classes the lineage repeatedly hit and could
    # not patch away. The child should avoid the path, not relearn the pain.
    for fc, count in sorted(recurring_failures.items()):
        if count >= 3:
            lessons.append(
                Lesson(
                    id=f"g{generation}-avoid-{fc}",
                    kind="avoid",
                    scope=f"failure:{fc}",
                    statement=(
                        f"the {fc!r} failure recurred {count}x and resisted "
                        f"self-modification; prefer a path that does not hit it"
                    ),
                    payload={"failure_class": fc, "observed_count": count},
                    rationale=(
                        "distilled at reproduction from sustained, "
                        "mutation-resistant recurrence"
                    ),
                    born_generation=generation,
                )
            )

    for fc in sorted(dead_ends):
        lessons.append(
            Lesson(
                id=f"g{generation}-deadend-{fc}",
                kind="avoid",
                scope=f"mutation:{fc}",
                statement=f"self-modification against {fc!r} was archived as a dead end",
                payload={"failure_class": fc},
                rationale="negative lesson carried so the dead end is not re-derived",
                born_generation=generation,
            )
        )

    # Positive lesson: the archetype that actually paid, stated as a soft
    # prefer (a prior, never a hard rule).
    if successful_strategy_id and profit_usdc > 0:
        lessons.append(
            Lesson(
                id=f"g{generation}-prefer-{successful_strategy_id}",
                kind="prefer",
                scope=f"strategy:{successful_strategy_id}",
                statement=(
                    f"{successful_strategy_id!r} was net-profitable "
                    f"(+${profit_usdc:.2f}) this generation; a reasonable first try"
                ),
                payload={"strategy_id": successful_strategy_id,
                         "profit_usdc": round(profit_usdc, 2)},
                rationale="distilled from a profit window that survived measurement",
                born_generation=generation,
            )
        )

    _assert_legible_and_safe(lessons, generation)
    return lessons


def _assert_legible_and_safe(lessons: list[Lesson], generation: int) -> None:
    for l in lessons:
        if l.born_generation != generation:
            raise DistillationError("lesson misattributed to wrong generation")
        if not l.statement.strip() or not l.rationale.strip():
            raise DistillationError(f"lesson {l.id} is not legible (empty text)")
        # A lesson may never be a vehicle for weakening the constitution.
        blob = (l.statement + " " + str(l.payload)).lower()
        for forbidden in ("raise cap", "disable kill", "skip audit",
                          "ignore policy", "bypass"):
            if forbidden in blob:
                raise DistillationError(
                    f"lesson {l.id} attempts a constitutional weakening"
                )
