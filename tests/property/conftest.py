"""Auto-mark every test under tests/property/ with ``property``. See doc 26."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    mark = pytest.mark.property
    for item in items:
        if "/tests/property/" in str(item.fspath):
            item.add_marker(mark)
