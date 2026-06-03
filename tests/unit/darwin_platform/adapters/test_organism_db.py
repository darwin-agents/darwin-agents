"""Real psycopg-backed per-Darwin organism database (doc 22 §"Per-Darwin
databases").

These tests inject a recording psycopg-shaped fake so the backend is exercised
end-to-end (CREATE ROLE / CREATE DATABASE / read-only grants / teardown DDL)
without a real Postgres. The load-bearing property is *database-boundary*
isolation: each Darwin gets its own database owned by its own login role, plus
a read-only dashboard role — not a shared database behind row filters.
"""

from __future__ import annotations

import pytest

from darwin_platform.adapters.organism_db import (
    OrganismDbError,
    RealOrganismDbBackend,
    build_organism_db_backend,
)
from darwin_platform.tenancy.namespace_provisioner import OrganismDbBackend


# -- recording psycopg-shaped fake ----------------------------------------
class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, sql, params=None) -> None:
        self._conn.statements.append((" ".join(sql.split()), params))
        for needle, exc in self._conn.fail_on.items():
            if needle in sql:
                raise exc


class _Conn:
    def __init__(self, dsn: str, registry: "_Registry") -> None:
        self.dsn = dsn
        self.statements: list[tuple] = []
        self.closed = False
        self.fail_on = registry.fail_on
        registry.conns.append(self)

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        self.closed = True


class _Registry:
    """Captures every connection opened + every statement run across them."""

    def __init__(self) -> None:
        self.conns: list[_Conn] = []
        self.fail_on: dict[str, Exception] = {}

    def connect(self, dsn: str) -> _Conn:
        return _Conn(dsn, self)

    def all_statements(self) -> list[str]:
        return [s for c in self.conns for (s, _) in c.statements]

    def dsns(self) -> list[str]:
        return [c.dsn for c in self.conns]


def _backend(reg: _Registry, **kw) -> RealOrganismDbBackend:
    return RealOrganismDbBackend(
        admin_dsn="postgresql://admin:pw@db-host:5432/postgres",
        connect=reg.connect,
        password_factory=lambda: "secretpw",
        **kw,
    )


# -- conformance ----------------------------------------------------------
def test_conforms_to_protocol():
    reg = _Registry()
    assert isinstance(_backend(reg), OrganismDbBackend)


# -- create_database ------------------------------------------------------
def test_create_database_runs_role_db_and_readonly_ddl():
    reg = _Registry()
    b = _backend(reg)
    db, role, dsn = b.create_database(darwin_id="darwin-AB-12")

    # Deterministic, sanitised names.
    assert db == "organism_darwin_ab_12"
    assert role == "role_darwin_ab_12"
    # The DSN carries the owner role's creds + the child endpoint + db.
    assert dsn == "postgresql://role_darwin_ab_12:secretpw@db-host:5432/organism_darwin_ab_12"

    stmts = reg.all_statements()
    assert any(s.startswith('CREATE ROLE "role_darwin_ab_12" LOGIN') for s in stmts)
    assert any(s.startswith('CREATE ROLE "role_darwin_ab_12_ro" LOGIN') for s in stmts)
    assert any(
        s == 'CREATE DATABASE "organism_darwin_ab_12" OWNER "role_darwin_ab_12"'
        for s in stmts
    )
    assert any('GRANT CONNECT ON DATABASE' in s for s in stmts)


def test_owner_role_created_before_database():
    """CREATE DATABASE ... OWNER needs the owner role to already exist."""
    reg = _Registry()
    _backend(reg).create_database(darwin_id="d1")
    stmts = reg.all_statements()
    owner_idx = next(i for i, s in enumerate(stmts) if s.startswith('CREATE ROLE "role_d1" LOGIN'))
    db_idx = next(i for i, s in enumerate(stmts) if s.startswith('CREATE DATABASE'))
    assert owner_idx < db_idx


def test_readonly_grants_run_inside_the_new_database():
    reg = _Registry()
    b = _backend(reg)
    b.create_database(darwin_id="d2")
    # The grants connect to a DSN re-pointed at the new database, not postgres.
    grant_conn = [c for c in reg.conns if c.dsn.endswith("/organism_d2")]
    assert grant_conn, "no admin connection was opened against the new db"
    grant_sql = " ".join(s for c in grant_conn for (s, _) in c.statements)
    assert 'GRANT USAGE ON SCHEMA public TO "role_d2_ro"' in grant_sql
    assert 'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "role_d2_ro"' in grant_sql
    assert 'ALTER DEFAULT PRIVILEGES FOR ROLE "role_d2"' in grant_sql


def test_readonly_credentials_recorded_for_dashboard_sealing():
    reg = _Registry()
    b = _backend(reg)
    db, _, _ = b.create_database(darwin_id="d3")
    creds = b.readonly_credentials[db]
    assert creds["role"] == "role_d3_ro"
    assert creds["dsn"].startswith("postgresql://role_d3_ro:secretpw@db-host:5432/organism_d3")


def test_readonly_role_can_be_disabled():
    reg = _Registry()
    b = _backend(reg, create_readonly_role=False)
    db, _, _ = b.create_database(darwin_id="d4")
    stmts = reg.all_statements()
    assert not any("_ro" in s for s in stmts)
    assert db not in b.readonly_credentials


# -- child endpoint override ----------------------------------------------
def test_child_host_port_override_for_pod_facing_dsn():
    reg = _Registry()
    b = RealOrganismDbBackend(
        admin_dsn="postgresql://admin:pw@127.0.0.1:5432/postgres",
        child_host="organism-db.svc",
        child_port=6432,
        connect=reg.connect,
        password_factory=lambda: "pw2",
    )
    _, _, dsn = b.create_database(darwin_id="d5")
    assert "@organism-db.svc:6432/organism_d5" in dsn


def test_sslmode_appended_to_child_dsn():
    reg = _Registry()
    b = _backend(reg, child_sslmode="require")
    _, _, dsn = b.create_database(darwin_id="d6")
    assert dsn.endswith("/organism_d6?sslmode=require")


# -- drop_database --------------------------------------------------------
def test_drop_database_terminates_and_drops_idempotently():
    reg = _Registry()
    b = _backend(reg)
    b.create_database(darwin_id="d7")
    reg.conns.clear()  # focus on the teardown statements only
    b.drop_database(database_name="organism_d7", role_name="role_d7")
    stmts = reg.all_statements()
    assert any("pg_terminate_backend" in s for s in stmts)
    assert any(s == 'DROP DATABASE IF EXISTS "organism_d7"' for s in stmts)
    assert any(s == 'DROP ROLE IF EXISTS "role_d7_ro"' for s in stmts)
    assert any(s == 'DROP ROLE IF EXISTS "role_d7"' for s in stmts)


def test_drop_database_drops_db_before_owner_role():
    reg = _Registry()
    b = _backend(reg)
    b.drop_database(database_name="organism_d8", role_name="role_d8")
    stmts = reg.all_statements()
    db_idx = next(i for i, s in enumerate(stmts) if s.startswith("DROP DATABASE"))
    owner_idx = next(i for i, s in enumerate(stmts) if s == 'DROP ROLE IF EXISTS "role_d8"')
    assert db_idx < owner_idx


# -- failure handling -----------------------------------------------------
def test_create_failure_rolls_back_partial_state():
    reg = _Registry()
    reg.fail_on["CREATE DATABASE"] = RuntimeError("disk full")
    b = _backend(reg)
    with pytest.raises(OrganismDbError, match="failed to provision"):
        b.create_database(darwin_id="d9")
    # Rollback dropped the roles it had already created.
    stmts = reg.all_statements()
    assert any(s == 'DROP ROLE IF EXISTS "role_d9"' for s in stmts)


def test_connections_are_closed():
    reg = _Registry()
    b = _backend(reg)
    b.create_database(darwin_id="d10")
    assert all(c.closed for c in reg.conns)


def test_invalid_identifier_rejected_before_any_ddl():
    # A darwin id that sanitises to empty would be unsafe; _sanitise floors it
    # to "x", but a directly-bad drop target must still be rejected.
    reg = _Registry()
    b = _backend(reg)
    with pytest.raises(OrganismDbError, match="invalid"):
        b.drop_database(database_name="bad-name; DROP", role_name="role_x")


def test_requires_admin_dsn_or_connect():
    with pytest.raises(OrganismDbError, match="admin_dsn is required"):
        RealOrganismDbBackend()


# -- builder --------------------------------------------------------------
def test_builder_constructs_backend():
    b = build_organism_db_backend(admin_dsn="postgresql://a:b@h:5432/postgres")
    assert isinstance(b, RealOrganismDbBackend)
