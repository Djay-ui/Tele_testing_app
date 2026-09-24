"""A column called `primary`, `key` or `order` must not break its table.

The migration builds its own SELECT, and it built it out of bare names:

    SELECT id, name, primary, created_at FROM app.address_rel

`primary` is reserved, so MySQL answered

    1064 (42000): You have an error in your SQL syntax ... near
    'primary, created_at, updated_at, deleted_at FROM app.address_rel'

and reported the table as failed -- 17 tables in one real run, while
every table whose columns happened to avoid the reserved list migrated
fine, which is why it went unnoticed for so long.
"""
import pytest

from tgdatabridge.core import migrator, sharding
from tgdatabridge.core.schema_model import Column, Constraint, Schema, Table
from tgdatabridge.db.source_sql import qualified_table, quote_columns, quoter_for

# Names taken from the run that failed, plus the classics.
RESERVED = ["primary", "key", "order", "group", "index", "class", "function",
            "condition", "year_month", "select", "table", "from", "where"]


class _Source:
    """Named so quoter_for picks an engine, and records the SQL it is
    handed rather than running it."""

    def __init__(self, columns, rows=((1, "x"),)):
        self.columns = columns
        self.rows = rows
        self.queries = []

    def fetch_batches(self, sql, batch_size=5000):
        self.queries.append(sql)
        yield list(self.columns), list(self.rows)

    def execute(self, sql, params=None):
        self.queries.append(sql)
        return [(1, 100)]


class MySQLConnector(_Source):
    pass


class PostgresConnector(_Source):
    pass


class SqlServerConnector(_Source):
    pass


class OracleConnector(_Source):
    pass


class _Target:
    def __init__(self):
        self.inserted = []

    def insert_batch(self, table, columns, rows):
        self.inserted.append((table, columns, rows))


def _table(columns):
    return Table(name="address_rel", schema="app",
                 columns=[Column(name=c, data_type="VARCHAR2(50)") for c in columns])


# ------------------------------------------------------- the quoting itself

@pytest.mark.parametrize("connector,expected", [
    (MySQLConnector, "`primary`"),
    (PostgresConnector, '"primary"'),
    (SqlServerConnector, "[primary]"),
    (OracleConnector, '"primary"'),
])
def test_each_engine_gets_its_own_quote_character(connector, expected):
    source = connector(["primary"])
    assert quote_columns(source, ["primary"]) == expected
    assert quoter_for(source)("primary") == expected


def test_the_schema_is_quoted_too():
    """A MySQL database can legitimately be named
    3f8cddb7226647be97fe09fd0b094e5e."""
    source = MySQLConnector([])
    assert qualified_table(source, "address_rel", "3f8cddb7226647be9") == \
        "`3f8cddb7226647be9`.`address_rel`"
    assert qualified_table(source, "address_rel", None) == "`address_rel`"


def test_names_are_quoted_exactly_as_introspected():
    """Quoting makes case significant, so re-casing here would turn a
    working query into "column does not exist" on Oracle or PostgreSQL."""
    assert quote_columns(OracleConnector([]), ["EmployeeID"]) == '"EmployeeID"'
    assert qualified_table(OracleConnector([]), "BIG", "HR") == '"HR"."BIG"'


# ------------------------------------------------ the SELECT the migrator runs

def test_every_reserved_word_column_is_quoted_in_the_select():
    source = MySQLConnector(RESERVED, rows=[tuple(range(len(RESERVED)))])
    migrator.migrate_table(source, _Target(), _table(RESERVED))
    sql = source.queries[0]
    for name in RESERVED:
        assert f"`{name}`" in sql, f"{name} reached the server unquoted"
    assert "`app`.`address_rel`" in sql


def test_the_table_actually_migrates_now():
    columns = ["id", "name", "primary", "created_at"]
    source = MySQLConnector(columns, rows=[(1, "a", "b", "c")])
    target = _Target()
    result = migrator.migrate_table(source, target, _table(columns))
    assert result.succeeded and result.rows_copied == 1


@pytest.mark.parametrize("connector,opener", [
    (MySQLConnector, "`"), (PostgresConnector, '"'),
    (SqlServerConnector, "["), (OracleConnector, '"'),
])
def test_the_select_is_quoted_for_every_source_engine(connector, opener):
    columns = ["id", "order"]
    source = connector(columns, rows=[(1, 2)])
    migrator.migrate_table(source, _Target(), _table(columns))
    assert f"{opener}order" in source.queries[0]


# --------------------------------------------------------------- sharding

def _shardable():
    table = Table(name="big", schema="app", columns=[
        Column(name="order", data_type="NUMBER(10)", nullable=False),
        Column(name="name", data_type="VARCHAR2(50)"),
    ])
    table.constraints = [Constraint(name="pk", kind="PRIMARY KEY", columns=["order"])]
    table.row_count_estimate = 5_000_000
    return table


def test_the_min_max_probe_quotes_the_key_column():
    """A primary key called `order` made even the probe a syntax error,
    which silently turned sharding off for that table."""
    source = MySQLConnector([])
    source.count_rows = lambda name, schema=None: 5_000_000
    sharding.plan_shards(source, _shardable(), max_shards=4)
    assert "MIN(`order`)" in source.queries[0]
    assert "`app`.`big`" in source.queries[0]


def test_the_shard_predicates_quote_the_key_column():
    source = MySQLConnector([])
    source.count_rows = lambda name, schema=None: 5_000_000
    shards = sharding.plan_shards(source, _shardable(), max_shards=4)
    assert len(shards) > 1, "this table should shard"
    for shard in shards:
        if shard.where_sql:
            assert "`order`" in shard.where_sql
            assert " order " not in f" {shard.where_sql} "


def test_build_shards_on_its_own_still_leaves_names_alone():
    """It is exported for testing the range arithmetic without a
    connector, so it must not need one."""
    shards = sharding.build_shards(_shardable(), "id", 1, 1000, 4)
    assert any(s.where_sql and "id" in s.where_sql for s in shards)
