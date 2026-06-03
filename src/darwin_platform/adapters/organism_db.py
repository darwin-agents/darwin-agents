"""Real per-Darwin organism-database backend (docs/engineering/22 §"Per-Darwin
databases", doc 07 §"Data isolation").

The :class:`darwin_platform.tenancy.NamespaceProvisioner` provisions three
per-Darwin namespaces (K8s + Vault + organism DB) through small backend
Protocols. Until now the only :class:`OrganismDbBackend` was the in-memory
``StubOrganismDbBackend``, which returns *symbolic* database/role names and a
fake DSN — so a Darwin's "organism database" (where its business data lives and
which doc 07/22 promise survives the Darwin's death and belongs to the owner)
had no backend to survive on. The docs-vs-impl audit flagged this: "per-Darwin
organism DB provisioning is symbolic — no CREATE DATABASE/CREATE ROLE/read-only
dashboard role exists".

This module ships the real backend. On the shared organism Postgres cluster it
runs, as the cluster admin:

- ``CREATE ROLE`` for the Darwin's **owner** role (LOGIN, NOSUPERUSER /
  NOCREATEDB / NOCREATEROLE) — the role the organism connects as to read+write
  its own data;
- ``CREATE DATABASE owner=<owner role>`` — a private database, so cross-tenant
  access is impossible *at the database boundary* (a separate database, not a
  shared one behind row filters);
- a **read-only dashboard role** (the audit's missing piece): a second LOGIN
  role granted ``CONNECT`` + ``USAGE`` + ``SELECT`` on the organism's schema,
  plus ``ALTER DEFAULT PRIVILEGES FOR ROLE <owner>`` so every table the owner
  later creates is automatically readable. This is what lets the operator
  dashboard's "Data browser" show the Darwin's *business* data without write
  access — the doc-08 promise.

The connection primitive is injected (``connect``) so the whole backend is
unit-testable against a recording fake and never touches a real Postgres in
tests. The admin DSN it uses needs ``CREATEDB`` + ``CREATEROLE`` (the
organism-cluster admin role), never a per-Darwin one.

``CREATE DATABASE`` cannot run inside a transaction block, so the admin
connection must be autocommit — the default ``connect`` sets it; an injected
connection is expected to do the same.
"""

from __future__ import annotations

import re
import secrets
from typing import Any, Callable
from urllib.parse import urlsplit


class OrganismDbError(Exception):
    """A per-Darwin organism-database admin operation failed."""


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str, *, what: str) -> str:
    """Reject anything that isn't a plain SQL identifier. Role + database
    names are interpolated into DDL (they cannot be bound parameters), so this
    guards the one place a darwin id reaches the SQL string."""
    if not _IDENTIFIER_RE.match(name or ""):
        raise OrganismDbError(
            f"invalid {what} {name!r}: must match [A-Za-z_][A-Za-z0-9_]*"
        )
    return name


def _sanitise(darwin_id: str) -> str:
    """Lower-case, ``-``/``.``→``_`` so a uuid-ish darwin id becomes a legal
    SQL identifier fragment. Length is capped well under Postgres' 63-byte
    identifier limit so the ``organism_``/``role_`` prefixes + the ``_ro``
    suffix never overflow it."""
    safe = re.sub(r"[^a-z0-9_]", "_", str(darwin_id).lower())
    safe = safe.strip("_")
    return safe[:40] or "x"


class RealOrganismDbBackend:
    """psycopg-backed :class:`OrganismDbBackend` (doc 22).

    Args:
        admin_dsn: connection string for the organism cluster's admin role
            (needs CREATEDB + CREATEROLE). Used both to run the DDL and — by
            swapping user/password/dbname — as the template for the per-Darwin
            DSN handed back to the provisioner. Ignored when ``connect`` is
            injected *and* ``child_host`` is given.
        child_host / child_port: host/port to embed in the per-Darwin DSN.
            Default: parsed from ``admin_dsn`` (URL-style DSNs only). Set these
            when the admin connects over a different address than the Darwin
            pods will (e.g. admin over localhost, pods over a service name).
        child_sslmode: optional ``sslmode`` appended to per-Darwin DSNs.
        create_readonly_role: provision the read-only dashboard role
            (default True). The owner tuple is always returned; the read-only
            credentials are recorded on :attr:`readonly_credentials` keyed by
            database name for a caller that wants to seal them in the Darwin's
            vault for the dashboard.
        connect: test seam — ``connect(dsn) -> connection`` returning a
            psycopg-shaped autocommit connection. Default: real psycopg.
        password_factory: test seam for deterministic passwords.
    """

    def __init__(
        self,
        *,
        admin_dsn: str | None = None,
        child_host: str | None = None,
        child_port: int | None = None,
        child_sslmode: str | None = None,
        create_readonly_role: bool = True,
        connect: Callable[[str], Any] | None = None,
        password_factory: Callable[[], str] | None = None,
    ) -> None:
        if connect is None and not admin_dsn:
            raise OrganismDbError(
                "admin_dsn is required when no connect callable is injected"
            )
        self._admin_dsn = admin_dsn or ""
        self._create_ro = bool(create_readonly_role)
        self._connect = connect or self._default_connect
        self._password_factory = password_factory or (
            lambda: secrets.token_urlsafe(24)
        )
        host, port = self._resolve_child_endpoint(child_host, child_port)
        self._child_host = host
        self._child_port = port
        self._child_sslmode = child_sslmode
        # database name -> {"role", "password", "dsn"} for the read-only role.
        self.readonly_credentials: dict[str, dict[str, str]] = {}

    # -- OrganismDbBackend surface ----------------------------------------
    def create_database(self, *, darwin_id: str) -> tuple[str, str, str]:
        """Provision the Darwin's private database, owner role, and (optional)
        read-only dashboard role. Returns ``(database, owner_role, dsn)`` where
        ``dsn`` carries the owner role's credentials."""
        if not darwin_id:
            raise OrganismDbError("darwin_id is required")
        seg = _sanitise(darwin_id)
        db = _validate_identifier(f"organism_{seg}", what="database name")
        owner = _validate_identifier(f"role_{seg}", what="role name")
        ro_role = _validate_identifier(f"{owner}_ro", what="role name")
        owner_pw = self._password_factory()
        ro_pw = self._password_factory()

        # Admin-side DDL. ``CREATE DATABASE`` forbids a transaction block, so
        # the connection must be autocommit (the default factory sets it).
        try:
            conn = self._connect(self._admin_dsn)
        except Exception as exc:
            raise OrganismDbError(
                f"failed to connect to organism cluster admin: {exc}"
            ) from exc
        try:
            with conn.cursor() as cur:
                # Owner role first — CREATE DATABASE ... OWNER needs it to exist.
                cur.execute(
                    f'CREATE ROLE "{owner}" LOGIN PASSWORD %s '
                    f"NOSUPERUSER NOCREATEDB NOCREATEROLE",
                    (owner_pw,),
                )
                if self._create_ro:
                    cur.execute(
                        f'CREATE ROLE "{ro_role}" LOGIN PASSWORD %s '
                        f"NOSUPERUSER NOCREATEDB NOCREATEROLE",
                        (ro_pw,),
                    )
                cur.execute(f'CREATE DATABASE "{db}" OWNER "{owner}"')
                if self._create_ro:
                    cur.execute(
                        f'GRANT CONNECT ON DATABASE "{db}" TO "{ro_role}"'
                    )
        except Exception as exc:
            # Best-effort cleanup of a half-created tenant so a retry is clean.
            self._safe_close(conn)
            self._rollback_partial(db, owner, ro_role)
            raise OrganismDbError(
                f"failed to provision organism DB for {darwin_id!r}: {exc}"
            ) from exc
        finally:
            self._safe_close(conn)

        # Read-only grants must run *inside* the new database (USAGE/SELECT and
        # default privileges are database-local). A fresh admin connection to
        # the new db; failures here are non-fatal — the DB + owner already
        # exist and work, the dashboard role just won't see rows yet.
        if self._create_ro:
            self._grant_readonly(db, owner, ro_role)
            self.readonly_credentials[db] = {
                "role": ro_role,
                "password": ro_pw,
                "dsn": self._child_dsn(ro_role, ro_pw, db),
            }

        return (db, owner, self._child_dsn(owner, owner_pw, db))

    def drop_database(self, *, database_name: str, role_name: str) -> None:
        """Drop the Darwin's database + owner role + read-only role. Best
        effort + idempotent (``IF EXISTS``) — the deferred reaper may run long
        after the pod is gone."""
        db = _validate_identifier(database_name, what="database name")
        owner = _validate_identifier(role_name, what="role name")
        ro_role = f"{owner}_ro"
        try:
            conn = self._connect(self._admin_dsn)
        except Exception as exc:
            raise OrganismDbError(
                f"failed to connect to organism cluster admin: {exc}"
            ) from exc
        errors: list[str] = []
        try:
            with conn.cursor() as cur:
                # Kick any lingering sessions so DROP DATABASE isn't refused.
                self._exec_safe(
                    cur,
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (db,),
                    errors,
                )
                self._exec_safe(cur, f'DROP DATABASE IF EXISTS "{db}"', None, errors)
                # Roles can only be dropped once nothing they own remains; the
                # database is gone, so this succeeds. ``ro`` may not exist.
                self._exec_safe(cur, f'DROP ROLE IF EXISTS "{ro_role}"', None, errors)
                self._exec_safe(cur, f'DROP ROLE IF EXISTS "{owner}"', None, errors)
        finally:
            self._safe_close(conn)
        self.readonly_credentials.pop(db, None)
        if errors:
            raise OrganismDbError(
                f"organism DB teardown for {db!r} had errors: {'; '.join(errors)}"
            )

    # -- internals --------------------------------------------------------
    def _grant_readonly(self, db: str, owner: str, ro_role: str) -> None:
        """Inside the new database, grant the read-only role schema access +
        SELECT on existing and future owner-created tables. Non-fatal."""
        ro_dsn = self._admin_dsn_for_db(db)
        try:
            conn = self._connect(ro_dsn)
        except Exception:
            return
        try:
            with conn.cursor() as cur:
                for stmt in (
                    f'GRANT USAGE ON SCHEMA public TO "{ro_role}"',
                    f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{ro_role}"',
                    # Future tables the OWNER creates: default privileges are
                    # keyed by the creating role, so scope this to the owner.
                    f'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" IN SCHEMA '
                    f'public GRANT SELECT ON TABLES TO "{ro_role}"',
                ):
                    self._exec_safe(cur, stmt, None, None)
        finally:
            self._safe_close(conn)

    def _rollback_partial(self, db: str, owner: str, ro_role: str) -> None:
        try:
            conn = self._connect(self._admin_dsn)
        except Exception:
            return
        try:
            with conn.cursor() as cur:
                self._exec_safe(cur, f'DROP DATABASE IF EXISTS "{db}"', None, None)
                self._exec_safe(cur, f'DROP ROLE IF EXISTS "{ro_role}"', None, None)
                self._exec_safe(cur, f'DROP ROLE IF EXISTS "{owner}"', None, None)
        finally:
            self._safe_close(conn)

    @staticmethod
    def _exec_safe(cur: Any, sql: str, params, errors: list[str] | None) -> None:
        try:
            cur.execute(sql, params) if params is not None else cur.execute(sql)
        except Exception as exc:  # noqa: BLE001 - collected, not swallowed silently
            if errors is not None:
                errors.append(str(exc))

    @staticmethod
    def _safe_close(conn: Any) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def _default_connect(self, dsn: str) -> Any:
        import psycopg

        return psycopg.connect(dsn, autocommit=True)

    def _resolve_child_endpoint(
        self, child_host: str | None, child_port: int | None
    ) -> tuple[str, int]:
        if child_host and child_port:
            return child_host, int(child_port)
        host, port = self._parse_endpoint(self._admin_dsn)
        return (child_host or host, int(child_port or port))

    @staticmethod
    def _parse_endpoint(dsn: str) -> tuple[str, int]:
        """Pull (host, port) out of a URL-style DSN. Keyword DSNs aren't
        parsed — pass ``child_host``/``child_port`` explicitly for those."""
        try:
            parts = urlsplit(dsn)
            if parts.scheme and parts.hostname:
                return parts.hostname, int(parts.port or 5432)
        except Exception:
            pass
        raise OrganismDbError(
            "could not parse host/port from admin_dsn; pass child_host + "
            "child_port explicitly (keyword-style DSNs are not parsed)"
        )

    def _child_dsn(self, role: str, password: str, db: str) -> str:
        dsn = (
            f"postgresql://{role}:{password}@{self._child_host}:"
            f"{self._child_port}/{db}"
        )
        if self._child_sslmode:
            dsn += f"?sslmode={self._child_sslmode}"
        return dsn

    def _admin_dsn_for_db(self, db: str) -> str:
        """The admin DSN re-pointed at ``db`` (for the in-database grants).
        URL-style only; falls back to the raw admin DSN if unparseable (the
        grants then run against the admin's default db and quietly no-op)."""
        try:
            parts = urlsplit(self._admin_dsn)
            if parts.scheme and parts.netloc:
                return f"{parts.scheme}://{parts.netloc}/{db}"
        except Exception:
            pass
        return self._admin_dsn


def build_organism_db_backend(
    *,
    admin_dsn: str,
    child_host: str | None = None,
    child_port: int | None = None,
    child_sslmode: str | None = None,
    create_readonly_role: bool = True,
) -> RealOrganismDbBackend:
    """Construct the production psycopg-backed backend. Kept thin so the
    NamespaceProvisioner wiring builds it from CLI flags / env without
    importing psycopg at module load."""
    return RealOrganismDbBackend(
        admin_dsn=admin_dsn,
        child_host=child_host,
        child_port=child_port,
        child_sslmode=child_sslmode,
        create_readonly_role=create_readonly_role,
    )
