"""Read-only organism database reader (doc 08; the DARWIN_ORGANISM_DB_DSN
consumer that lets the data browser show a Darwin's business data).

Tests inject a fake psycopg-shaped connection so no Postgres is touched. The
load-bearing properties: it pins a READ ONLY transaction, validates the table
name as an identifier (no injection), and hard-caps the page size.
"""

from __future__ import annotations

import pytest

from darwin_platform.adapters.organism_reader import (
    OrganismDbReader,
    OrganismReaderError,
    build_organism_reader,
)


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
        s = sql.lower()
        if "information_schema.tables" in s:
            self._result = [{"table_name": "posts"}, {"table_name": "sales"}]
            self.description = [("table_name",)]
        elif "set transaction read only" in s:
            self._result = []
        elif s.startswith("select * from"):
            self.description = [("id",), ("title",)]
            self._result = [{"id": 1, "title": "hello"}]
        else:
            self._result = []

    def fetchall(self):
        return self._result


class _Conn:
    def __init__(self, dsn):
        self.dsn = dsn
        self.executed: list = []
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


class _Registry:
    def __init__(self):
        self.conns = []

    def connect(self, dsn):
        c = _Conn(dsn)
        self.conns.append(c)
        return c


def _reader(reg, **kw):
    return OrganismDbReader(
        dsn="postgresql://role_x_ro:pw@db/organism_x",
        connect=reg.connect, **kw,
    )


def test_list_tables_returns_public_tables():
    reg = _Registry()
    assert _reader(reg).list_tables() == ["posts", "sales"]


def test_read_rows_returns_columns_and_rows():
    reg = _Registry()
    out = _reader(reg).read_rows("posts", limit=10)
    assert out["table"] == "posts"
    assert out["columns"] == ["id", "title"]
    assert out["rows"] == [{"id": 1, "title": "hello"}]
    assert out["limit"] == 10 and out["offset"] == 0


def test_read_rows_pins_read_only_transaction():
    reg = _Registry()
    _reader(reg).read_rows("posts")
    stmts = [s for (s, _) in reg.conns[0].executed]
    assert any(s == "SET TRANSACTION READ ONLY" for s in stmts)


def test_read_rows_binds_limit_and_offset():
    reg = _Registry()
    _reader(reg).read_rows("posts", limit=25, offset=50)
    select = [(s, p) for (s, p) in reg.conns[0].executed if s.startswith("SELECT * FROM")]
    assert select and select[0][1] == (25, 50)


def test_table_name_is_validated_against_injection():
    reg = _Registry()
    with pytest.raises(OrganismReaderError, match="invalid table name"):
        _reader(reg).read_rows("posts; DROP TABLE sales")
    # Nothing was executed against the database.
    assert reg.conns == []


def test_page_size_is_hard_capped():
    reg = _Registry()
    out = _reader(reg, max_page=20).read_rows("posts", limit=10_000)
    assert out["limit"] == 20


def test_connection_is_closed():
    reg = _Registry()
    _reader(reg).list_tables()
    assert all(c.closed for c in reg.conns)


def test_requires_dsn():
    with pytest.raises(OrganismReaderError, match="dsn is required"):
        OrganismDbReader(dsn="")


def test_builder_constructs_reader():
    assert isinstance(build_organism_reader(dsn="postgresql://a:b@h/d"),
                      OrganismDbReader)
