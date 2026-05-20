"""Auto-mark every test under tests/integration/ with ``integration``. See doc 26."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    mark = pytest.mark.integration
    for item in items:
        if "/tests/integration/" in str(item.fspath):
            item.add_marker(mark)
