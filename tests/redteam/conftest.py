"""Auto-mark every test under tests/redteam/ with ``redteam``. See doc 26."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    mark = pytest.mark.redteam
    for item in items:
        if "/tests/redteam/" in str(item.fspath):
            item.add_marker(mark)
