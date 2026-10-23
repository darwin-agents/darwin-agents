"""Performance tier: latency, throughput, and memory budgets on hot paths.

Two kinds of check, both deterministic enough for shared CI runners:

* **Scaling** — the cost of an operation must not grow with history it is
  supposed to be independent of (O(1) treasury balance, O(limit) audit tail,
  flat-memory chain boot). Asserted as a ratio between a small and a 10×
  larger input, so machine speed cancels out.
* **Budgets** — absolute ceilings with ~10× headroom over a developer
  laptop, so a regression of an order of magnitude fails while runner noise
  does not. ``DARWIN_PERF_SCALE`` multiplies every budget (e.g. ``3`` on a
  slow runner).

Auto-marks every test ``performance`` *and* ``slow``: ``make test-perf`` /
``pytest -m performance`` runs the tier; the fast loop skips it. See doc 26.
"""

from __future__ import annotations

import os
import statistics
import time
import tracemalloc
from typing import Callable

import pytest

SCALE = float(os.environ.get("DARWIN_PERF_SCALE", "1"))


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "/tests/performance/" in str(item.fspath):
            item.add_marker(pytest.mark.performance)
            item.add_marker(pytest.mark.slow)


class Perf:
    scale = SCALE

    @staticmethod
    def best_of(fn: Callable[[], object], *, repeat: int = 5) -> float:
        """Fastest wall-clock seconds over ``repeat`` runs (the min is the
        least noisy estimator of intrinsic cost)."""
        times = []
        for _ in range(repeat):
            t = time.perf_counter()
            fn()
            times.append(time.perf_counter() - t)
        return min(times)

    @staticmethod
    def samples(fn: Callable[[], object], *, n: int) -> list[float]:
        out = []
        for _ in range(n):
            t = time.perf_counter()
            fn()
            out.append(time.perf_counter() - t)
        return out

    @staticmethod
    def p95(samples: list[float]) -> float:
        return statistics.quantiles(samples, n=20)[-1]

    @staticmethod
    def peak_bytes(fn: Callable[[], object]) -> int:
        """Peak Python heap allocated while ``fn`` runs."""
        tracemalloc.start()
        try:
            fn()
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def budget(self, seconds: float) -> float:
        return seconds * self.scale


@pytest.fixture(scope="session")
def perf() -> Perf:
    return Perf()
