"""Read-only reader for a Darwin's organism database (docs/engineering/08, 22).

The :class:`~darwin_platform.adapters.organism_db.RealOrganismDbBackend`
provisions each Darwin a *private* Postgres database (its business data) plus a
**read-only dashboard role**. The pod gets the owner DSN
(``DARWIN_ORGANISM_DB_DSN``); the operator dashboard's "Data browser" needs the
*read-only* DSN so it can show the Darwin's real business data — not the
control-plane rows the audit flagged it as showing today.

This module is that consumer. :class:`OrganismDbReader`:

- connects with the **read-only role's** DSN (granted only ``SELECT``), and
  additionally pins the session to a read-only transaction as defence in depth,
  so a buggy/compromised caller still cannot mutate organism data;
- lists the public tables and reads a bounded page of rows from one;
- validates the table name as a plain identifier (it is interpolated into the
  query, not bindable) and hard-caps the page size, so the data browser cannot
  be turned into an unbounded scan or an injection vector.

The connection is injected (``connect``) so the reader is fully unit-testable
against a fake and never touches a real Postgres in tests.
"""

from __future__ import annotations

import re
from typing import Any, Callable


class OrganismReaderError(Exception):
    """A read against a Darwin's organism database failed or was refused."""


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_PAGE = 1000


def _validate_identifier(name: str, *, what: str) -> str:
    if not _IDENTIFIER_RE.match(name or ""):
        raise OrganismReaderError(
            f"invalid {what} {name!r}: must match [A-Za-z_][A-Za-z0-9_]*"
        )
    return name


class OrganismDbReader:
    """Read-only window onto one Darwin's organism database.

    Args:
        dsn: the **read-only** role's connection string for the Darwin's
            database (from the NamespaceProvisioner). Using the read-only role
            means even a query bug cannot write.
        connect: ``connect(dsn) -> connection`` returning a psycopg-shaped
            connection (default: real psycopg, autocommit, dict rows).
        max_page: hard cap on rows returned per :meth:`read_rows` call.
    """

    def __init__(
        self,
        *,
        dsn: str,
        connect: Callable[[str], Any] | None = None,
        max_page: int = 100,
    ) -> None:
        if not dsn:
            raise OrganismReaderError("dsn is required")
        self._dsn = dsn
        self._connect = connect or self._default_connect
        self._max_page = min(int(max_page), _MAX_PAGE)

    # -- reads -------------------------------------------------------------
    def list_tables(self) -> list[str]:
        """Public-schema table names in the organism database, sorted."""
        conn = self._open()
        try:
            with conn.cursor() as cur:
                self._read_only(cur)
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                )
                rows = cur.fetchall()
        except OrganismReaderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OrganismReaderError(f"failed to list tables: {exc}") from exc
        finally:
            self._safe_close(conn)
        return [self._first(r) for r in rows]

    def read_rows(
        self, table: str, *, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        """A bounded page of rows from ``table``. Returns
        ``{"table", "columns", "rows", "limit", "offset"}``. The table name is
        validated as an identifier; ``limit`` is capped at ``max_page``."""
        table = _validate_identifier(table, what="table name")
        limit = max(1, min(int(limit), self._max_page))
        offset = max(0, int(offset))
        conn = self._open()
        try:
            with conn.cursor() as cur:
                self._read_only(cur)
                # Identifier can't be a bound param; it's validated above.
                # LIMIT/OFFSET are bound. The read-only txn blocks any write.
                cur.execute(
                    f'SELECT * FROM "{table}" LIMIT %s OFFSET %s',
                    (limit, offset),
                )
                rows = cur.fetchall()
                columns = (
                    [d[0] for d in cur.description] if cur.description else []
                )
        except OrganismReaderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OrganismReaderError(
                f"failed to read {table!r}: {exc}"
            ) from exc
        finally:
            self._safe_close(conn)
        return {
            "table": table,
            "columns": columns,
            "rows": [self._rowdict(r, columns) for r in rows],
            "limit": limit,
            "offset": offset,
        }

    # -- internals ---------------------------------------------------------
    def _open(self) -> Any:
        try:
            return self._connect(self._dsn)
        except Exception as exc:  # noqa: BLE001
            raise OrganismReaderError(
                f"failed to connect to organism database: {exc}"
            ) from exc

    @staticmethod
    def _read_only(cur: Any) -> None:
        # Defence in depth beyond the read-only role: pin the transaction.
        try:
            cur.execute("SET TRANSACTION READ ONLY")
        except Exception:
            # Some drivers/fakes don't support it; the read-only role still
            # blocks writes, so this is best-effort hardening, not the gate.
            pass

    def _default_connect(self, dsn: str) -> Any:
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)

    @staticmethod
    def _first(row: Any) -> str:
        if isinstance(row, dict):
            return str(next(iter(row.values())))
        if isinstance(row, (list, tuple)):
            return str(row[0])
        return str(row)

    @staticmethod
    def _rowdict(row: Any, columns: list[str]) -> dict[str, Any]:
        if isinstance(row, dict):
            return dict(row)
        if isinstance(row, (list, tuple)):
            return {columns[i] if i < len(columns) else str(i): v
                    for i, v in enumerate(row)}
        return {"value": row}

    @staticmethod
    def _safe_close(conn: Any) -> None:
        try:
            conn.close()
        except Exception:
            pass


def build_organism_reader(
    *, dsn: str, max_page: int = 100,
) -> OrganismDbReader:
    """Construct the production read-only organism reader from a DSN."""
    return OrganismDbReader(dsn=dsn, max_page=max_page)
