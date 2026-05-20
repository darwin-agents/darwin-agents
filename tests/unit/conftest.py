"""Auto-mark every test under tests/unit/ with ``unit`` so callers can use
``pytest -m unit`` in addition to ``pytest tests/unit``. See doc 26."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    mark = pytest.mark.unit
    for item in items:
        if "/tests/unit/" in str(item.fspath):
            item.add_marker(mark)
