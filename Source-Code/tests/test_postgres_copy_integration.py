"""PostgresConnector.insert_batch against a **real** PostgreSQL server.

Why this file exists
--------------------
Every other test in this suite fakes the connection, which is what lets
1,300-odd tests run in three seconds with no database installed. That
trade has one blind spot, and it cost a real migration: binary COPY's
wire format is only validated by an actual PostgreSQL backend. A fake
cursor whose `write_row` appends to a list will happily accept values
that the server rejects outright.

That is exactly what happened. `insert_batch` issued
`COPY ... FROM STDIN (FORMAT BINARY)` without declaring column types, so
psycopg inferred an OID per value -- and its default dumper for a Python
`int` is **numeric**, which PostgreSQL refuses to accept into an int4,
int8 or bool column ("insufficient data left in message"). COPY is
all-or-nothing per batch, so every table with an integer primary key
failed completely: a MySQL -> PostgreSQL run reported *0 rows migrated*
with every non-empty table listed as failed. Fifteen unit tests covering
insert_batch all passed throughout, because none of them encoded
anything.

These tests therefore talk to a real server or skip. They are the only
place that can prove the wire format is right.

Running them
------------
Set TGSCT_TEST_PG_DSN to a libpq connection string for a *disposable*
database and run pytest as usual::

    TGSCT_TEST_PG_DSN="host=/tmp port=5433 user=postgres dbname=postgres" pytest tests/

Without that variable every test here skips, so the ordinary suite is
unchanged for anyone without a server to hand. The tables it creates are
named tgdatabridge_it_* and dropped on the way in and out.
"""
import datetime
import decimal
import os

import pytest

from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.db.postgres_connector import PostgresConnector

DSN = os.environ.get("TGSCT_TEST_PG_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="TGSCT_TEST_PG_DSN not set -- needs a real PostgreSQL server")


def _dsn_parts():
    parts = dict(p.split("=", 1) for p in DSN.split())
    return parts


@pytest.fixture
def connector():
    psycopg = pytest.importorskip("psycopg")
    parts = _dsn_parts()
    conn = PostgresConnector(ConnectionParams(
        host=parts.get("host", "localhost"),
        port=int(parts.get("port", 5432)),
        database=parts.get("dbname", "postgres"),
        username=parts.get("user", "postgres"),
        password=parts.get("password", ""),
        schema="public",
    ))
    conn.connect()
    yield conn
    conn.close()


def _make_table(connector, name, ddl):
    cur = connector._conn.cursor()
    cur.execute(f'DROP TABLE IF EXISTS "{name}"')
    cur.execute(f'CREATE TABLE "{name}" ({ddl})')
    cur.close()


def _count(connector, name):
    cur = connector._conn.cursor()
    cur.execute(f'SELECT count(*) FROM "{name}"')
    (n,) = cur.fetchone()
    cur.close()
    return n


# --------------------------------------------------------- the regression

@pytest.mark.parametrize("coltype,value", [
    ("integer", 1),                                   # MySQL INT/SMALLINT/TINYINT
    ("bigint", 1),                                    # MySQL BIGINT
    ("boolean", True),                                # MySQL TINYINT(1)
    ("numeric(10,2)", decimal.Decimal("1.50")),       # MySQL DECIMAL
    ("numeric(10,2)", 1),                             # an int landing in a numeric column
    ("varchar(100)", "Acme"),                         # MySQL VARCHAR
    ("text", "Acme"),                                 # MySQL TEXT
    ("timestamp", datetime.datetime(2026, 1, 1, 9, 30)),   # MySQL DATETIME
    ("date", datetime.date(2026, 1, 1)),              # MySQL DATE
    ("double precision", 1.5),                        # MySQL DOUBLE
])
def test_every_common_column_type_accepts_its_value(connector, coltype, value):
    """The integer, bigint and boolean cases are the ones that used to
    fail, and between them they cover the primary key of essentially
    every real table -- which is why the symptom was 0 rows migrated
    rather than a few bad tables."""
    _make_table(connector, "tgdatabridge_it_types", f"c {coltype}")
    connector.insert_batch("tgdatabridge_it_types", ["c"], [(value,)])
    assert _count(connector, "tgdatabridge_it_types") == 1

    cur = connector._conn.cursor()
    cur.execute('SELECT c FROM "tgdatabridge_it_types"')
    (stored,) = cur.fetchone()
    cur.close()
    assert stored == value


def test_a_realistic_mysql_shaped_row_round_trips(connector):
    """The whole shape at once, as MySQLConnector.fetch_batches would
    hand it over: int id, varchar, datetime, decimal, bool."""
    _make_table(
        connector, "tgdatabridge_it_companies",
        "id integer, company_name varchar(100), created_at timestamp, "
        "ratio numeric(10,2), active boolean")
    rows = [
        (1, "Acme", datetime.datetime(2026, 1, 1, 9, 30), decimal.Decimal("1.50"), True),
        (2, "Globex", datetime.datetime(2026, 2, 2, 10, 0), decimal.Decimal("2.25"), False),
    ]
    connector.insert_batch(
        "tgdatabridge_it_companies",
        ["id", "company_name", "created_at", "ratio", "active"], rows)

    cur = connector._conn.cursor()
    cur.execute('SELECT id, company_name, created_at, ratio, active FROM "tgdatabridge_it_companies" ORDER BY id')
    assert [tuple(r) for r in cur.fetchall()] == rows
    cur.close()


def test_nulls_are_written(connector):
    """A column that is NULL in every row of a batch gives psycopg nothing
    to infer a type from at all -- the catalogue lookup is what makes this
    unambiguous."""
    _make_table(connector, "tgdatabridge_it_nulls", "id integer, note text, amount numeric(10,2)")
    connector.insert_batch(
        "tgdatabridge_it_nulls", ["id", "note", "amount"], [(1, None, None), (2, None, None)])

    cur = connector._conn.cursor()
    cur.execute('SELECT id, note, amount FROM "tgdatabridge_it_nulls" ORDER BY id')
    assert [tuple(r) for r in cur.fetchall()] == [(1, None, None), (2, None, None)]
    cur.close()


def test_uppercase_source_names_reach_the_lowercased_target(connector):
    """Names arrive in the source's case; ddl_generator creates every
    Postgres object lowercased, so insert_batch has to fold too -- and the
    catalogue lookup must fold with it."""
    _make_table(connector, "tgdatabridge_it_case", "id integer, name varchar(50)")
    connector.insert_batch("TGSCT_IT_CASE", ["ID", "NAME"], [(1, "x")])
    assert _count(connector, "tgdatabridge_it_case") == 1


def test_a_large_batch_writes_every_row(connector):
    _make_table(connector, "tgdatabridge_it_bulk", "id integer, name varchar(50)")
    rows = [(i, f"name-{i}") for i in range(5000)]
    connector.insert_batch("tgdatabridge_it_bulk", ["id", "name"], rows)
    assert _count(connector, "tgdatabridge_it_bulk") == 5000


# ------------------------------------------------------------- fallback

def test_text_fallback_writes_the_same_rows(connector):
    """The safety net: if binary is unavailable for any reason, the batch
    must still land rather than failing the migration."""
    _make_table(connector, "tgdatabridge_it_fallback", "id integer, name varchar(50)")
    connector._copy_text_only = True          # force the fallback path
    rows = [(1, "a"), (2, "b")]
    connector.insert_batch("tgdatabridge_it_fallback", ["id", "name"], rows)

    cur = connector._conn.cursor()
    cur.execute('SELECT id, name FROM "tgdatabridge_it_fallback" ORDER BY id')
    assert [tuple(r) for r in cur.fetchall()] == rows
    cur.close()


def test_unknown_table_types_fall_back_instead_of_raising(connector):
    """_column_types returns None for anything it cannot resolve, which
    routes the write down the text path rather than guessing an OID."""
    _make_table(connector, "tgdatabridge_it_unknown", "id integer")
    assert connector._column_types("tgdatabridge_it_unknown", ["nope"]) is None
    assert connector._column_types("no_such_table_at_all", ["id"]) is None
    # ...and the connection is still usable afterwards.
    connector.insert_batch("tgdatabridge_it_unknown", ["id"], [(1,)])
    assert _count(connector, "tgdatabridge_it_unknown") == 1


def test_column_types_are_cached_per_table(connector):
    _make_table(connector, "tgdatabridge_it_cache", "id integer, name varchar(50)")
    connector.insert_batch("tgdatabridge_it_cache", ["id", "name"], [(1, "a")])
    assert connector._column_type_cache["tgdatabridge_it_cache"] == {"id": "int4", "name": "varchar"}
    # A second batch reuses the cache rather than re-querying.
    connector.insert_batch("tgdatabridge_it_cache", ["id", "name"], [(2, "b")])
    assert _count(connector, "tgdatabridge_it_cache") == 2


def test_validation_helpers_agree_with_what_was_written(connector):
    """count_rows/checksum_rows back post-migration validation, so they
    have to see the rows COPY actually wrote."""
    _make_table(connector, "tgdatabridge_it_validate", "id integer, name varchar(50)")
    rows = [(1, "a"), (2, "b"), (3, "c")]
    connector.insert_batch("tgdatabridge_it_validate", ["id", "name"], rows)
    assert connector.count_rows("tgdatabridge_it_validate") == 3
    assert isinstance(connector.checksum_rows("tgdatabridge_it_validate", ["id", "name"]), int)
