"""Guards the Swagger/OpenAPI documentation stays complete and in sync.

The per-controller (tag) blurbs and per-endpoint summaries/descriptions live
in ``darwin_platform.routers._openapi``. These tests fail loudly if someone
adds a route without documenting it, removes a route a doc entry still points
at, or mounts a router whose tag has no description — so the docs can't
silently rot.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.routing import APIRoute  # noqa: E402

from darwin_platform.api import build_app  # noqa: E402
from darwin_platform.routers import ALL_ROUTERS  # noqa: E402
from darwin_platform.routers._openapi import (  # noqa: E402
    ENDPOINTS,
    TAGS,
    apply_openapi_metadata,
)


def _route_keys(app) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                keys.add((method, route.path))
    return keys


def test_every_route_is_documented() -> None:
    """No HTTP operation may lack a summary/description entry."""
    app = build_app()
    undocumented = sorted(_route_keys(app) - set(ENDPOINTS))
    assert not undocumented, (
        "routes missing an _openapi.ENDPOINTS entry (add summary/description): "
        f"{undocumented}"
    )


def test_no_stale_endpoint_entries() -> None:
    """Every ENDPOINTS entry must map to a real route (catch renamed paths)."""
    app = build_app()
    stale = sorted(set(ENDPOINTS) - _route_keys(app))
    assert not stale, f"_openapi.ENDPOINTS entries with no matching route: {stale}"


def test_every_mounted_tag_has_a_description() -> None:
    """Every tag used by a mounted router must have a TAGS blurb."""
    described = {t["name"] for t in TAGS if t.get("description")}
    used = {tag for router in ALL_ROUTERS for tag in (router.tags or [])}
    missing = sorted(used - described)
    assert not missing, f"router tags without a description in _openapi.TAGS: {missing}"


def test_openapi_schema_fully_annotated() -> None:
    """The generated schema exposes a summary + description for every op."""
    app = build_app(enable_docs=True)
    schema = app.openapi()
    no_summary = []
    no_description = []
    for path, methods in schema["paths"].items():
        for method, op in methods.items():
            if not op.get("summary"):
                no_summary.append(f"{method.upper()} {path}")
            if not op.get("description"):
                no_description.append(f"{method.upper()} {path}")
    assert not no_summary, f"operations missing a summary: {no_summary}"
    assert not no_description, f"operations missing a description: {no_description}"


def test_apply_is_idempotent_and_safe_to_recall() -> None:
    """Re-applying metadata must not raise or change the result."""
    app = build_app(enable_docs=True)
    apply_openapi_metadata(app)  # second pass
    schema = app.openapi()
    # spot-check a representative operation survived intact
    spawn = schema["paths"]["/darwins"]["post"]
    assert spawn["summary"] == "Spawn a Darwin"
    assert spawn["description"]
