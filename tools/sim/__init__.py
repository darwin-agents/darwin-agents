"""The simulated world. NOT part of the genome (lives outside darwin_dna).

``run_sim`` is intentionally not re-exported here: it is the ``python -m
tools.sim.run_sim`` entry point, and importing it from the package __init__
makes the runpy double-import warning fire. Import it directly:
``from tools.sim.run_sim import run_simulation``.
"""

from .scenario import SCENARIOS, Scenario
from .world import RevenueModel, SimWorld

__all__ = ["SCENARIOS", "Scenario", "RevenueModel", "SimWorld"]
