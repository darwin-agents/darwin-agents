"""Postgres row-level security / tenant isolation (docs/engineering/07,
docs/engineering/23 §"Phase 2 RLS policies").

These exercise the v12 RLS migration + the per-Darwin scoping helpers on a
*real* Postgres — SQLite has no row-level security so the behaviour can't
be observed there. The doc-07 promise we verify:

    Every database connection sets the GUC (``app.current_darwin_id``) at
    session start; a query that forgets returns zero rows, not other
    tenants' data.

**Skipped when ``DARWIN_POSTGRES_TEST_DSN`` is not set** (same gate as
:mod:`test_persistence_postgres`). The connecting role must be a superuser
or table owner so the fixture can recreate the schema and the store can
write across tenants (the control-plane bypass); the per-Darwin path drops
to the restricted ``darwin_tenant`` role via ``SET ROLE`` so RLS bites.

Each test starts from a *fresh* ``public`` schema so the full migration
chain (including v12) re-runs and the policies are present on every table —
dropping individual tables would leave ``schema_version`` at 12 and skip
the RLS migration on re-create.
"""

from __future__ import annotations

import os

import pytest

DSN = os.environ.get("DARWIN_POSTGRES_TEST_DSN")
if not DSN:
    pytest.skip(
        "DARWIN_POSTGRES_TEST_DSN not set; skipping Postgres RLS tests",
        allow_module_level=True,
    )

psycopg = pytest.importorskip("psycopg")

from darwin_platform.runtime.persistence import (  # noqa: E402
    DEFAULT_TENANT_ROLE,
    PersistenceStore,
    _RLS_TENANT_TABLES,
)


def _reset_schema() -> None:
    """Drop and recreate the public schema so the next store open runs the
    whole migration chain (RLS policies included) against virgin tables."""
    conn = psycopg.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def store():
    _reset_schema()
    s = PersistenceStore(DSN, clock=lambda: 1_000.0)
    s.ensure_tenant_role()
    try:
        yield s
    finally:
        s.close()
        _reset_schema()


def _seed_two_tenants(store: PersistenceStore) -> None:
    """Write rows for d1 (x2) and d2 (x1) through the privileged store
    connection — which bypasses RLS, like the real control plane."""
    a = store.audit_log
    a.record(actor="alice", action="spawn", darwin_id="d1")
    a.record(actor="alice", action="kill", darwin_id="d1")
    a.record(actor="bob", action="spawn", darwin_id="d2")


# -- the migration actually installed the policies -------------------------
def test_v12_enables_rls_on_every_tenant_table(store):
    conn = psycopg.connect(DSN, row_factory=psycopg.rows.dict_row)
    try:
        for table in _RLS_TENANT_TABLES:
            row = conn.execute(
                "SELECT relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname = %s",
                (table,),
            ).fetchone()
            assert row is not None, f"{table} missing"
            assert row["relrowsecurity"], f"RLS not enabled on {table}"
            assert row["relforcerowsecurity"], f"RLS not forced on {table}"
            pol = conn.execute(
                "SELECT 1 FROM pg_policies "
                "WHERE tablename = %s AND policyname = %s",
                (table, f"{table}_tenant_isolation"),
            ).fetchone()
            assert pol is not None, f"policy missing on {table}"
    finally:
        conn.close()


# -- scoped session sees only its own tenant ------------------------------
def test_scoped_connection_sees_only_its_darwin(store):
    _seed_two_tenants(store)

    with store.tenant_connection("d1") as c:
        rows = c.execute(
            "SELECT darwin_id, action FROM audit_entries ORDER BY seq"
        ).fetchall()
    assert [(r["darwin_id"], r["action"]) for r in rows] == [
        ("d1", "spawn"),
        ("d1", "kill"),
    ]

    with store.tenant_connection("d2") as c:
        rows = c.execute("SELECT darwin_id FROM audit_entries").fetchall()
    assert [r["darwin_id"] for r in rows] == ["d2"]


# -- forgetting the GUC returns zero rows (the doc-07 promise) -------------
def test_session_without_guc_sees_zero_rows(store):
    _seed_two_tenants(store)

    conn = psycopg.connect(DSN, autocommit=True, row_factory=psycopg.rows.dict_row)
    try:
        # Drop to the restricted role but never set app.current_darwin_id.
        conn.execute(f'SET ROLE "{DEFAULT_TENANT_ROLE}"')
        rows = conn.execute("SELECT darwin_id FROM audit_entries").fetchall()
        assert rows == []
    finally:
        conn.close()


# -- the control plane (bypass) still sees everything ---------------------
def test_privileged_store_connection_sees_all_tenants(store):
    _seed_two_tenants(store)
    # The store's own connection is the superuser/owner role → bypasses RLS.
    assert len(store.audit_log.entries()) == 3


# -- a tenant cannot write into another tenant ----------------------------
def test_scoped_connection_cannot_insert_cross_tenant(store):
    with store.tenant_connection("d1") as c:
        # Its own row is accepted.
        c.execute(
            "INSERT INTO darwin_lifecycle"
            "(darwin_id, opening_usdc, spawned_at, alive, killed) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("d1", 1.0, 0.0, 1, 0),
        )
        # A row for another tenant violates the policy's WITH CHECK
        # (reused USING clause) → InsufficientPrivilege.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "INSERT INTO darwin_lifecycle"
                "(darwin_id, opening_usdc, spawned_at, alive, killed) "
                "VALUES (%s, %s, %s, %s, %s)",
                ("d2", 1.0, 0.0, 1, 0),
            )


# -- the restricted role really lacks BYPASSRLS ---------------------------
def test_tenant_role_is_not_bypassrls(store):
    conn = psycopg.connect(DSN, row_factory=psycopg.rows.dict_row)
    try:
        row = conn.execute(
            "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = %s",
            (DEFAULT_TENANT_ROLE,),
        ).fetchone()
        assert row is not None
        assert row["rolbypassrls"] is False
        assert row["rolsuper"] is False
    finally:
        conn.close()
