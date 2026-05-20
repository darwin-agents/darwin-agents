"""Auto-mark every test under tests/simulation/ with ``simulation`` *and*
``slow`` so ``pytest -m 'not slow'`` keeps skipping the heavy sim tier even
when individual files forgot the marker. See doc 26."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    simulation_mark = pytest.mark.simulation
    slow_mark = pytest.mark.slow
    for item in items:
        if "/tests/simulation/" in str(item.fspath):
            item.add_marker(simulation_mark)
            item.add_marker(slow_mark)
