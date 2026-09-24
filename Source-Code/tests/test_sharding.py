"""Tests for tgdatabridge.core.sharding -- splitting one large table into
disjoint row ranges so several workers can migrate it at once
(SCALE.md section 1.2).

The single most important property here is that a table's shards
*partition* it: every row matched by exactly one shard, no gaps and no
overlaps. A gap silently loses rows; an overlap silently duplicates them.
Both would survive a naive row-count check on a table that's also being
written to, so several tests below evaluate the generated predicates
against real values rather than just asserting on the SQL text.

Plain functions, no pytest -- this repo's hand-rolled runner has no
pytest available (see tests/test_cli_config.py's own note).
"""
from tgdatabridge.core import sharding
from tgdatabridge.core.schema_model import Column, Constraint, Table


def _table(name="BIG", pk_column="ID", pk_type="NUMBER(10)", with_pk=True, pk_columns=None):
    table = Table(name=name, schema="HR")
    table.columns = [
        Column(name="ID", data_type=pk_type),
        Column(name="NAME", data_type="VARCHAR2(50)"),
        Column(name="CODE", data_type="VARCHAR2(10)"),
    ]
    if with_pk:
        columns = pk_columns if pk_columns is not None else [pk_column]
        table.constraints = [Constraint(name=f"{name}_PK", kind="PRIMARY KEY", columns=columns)]
    return table


class _FakeSource:
    """Minimal SQL source: answers the MIN/MAX probe and a row count."""

    def __init__(self, low=1, high=1000, rows=5_000_000, explode=False):
        self.low, self.high, self.rows, self.explode = low, high, rows, explode
        self.executed = []

    def count_rows(self, table, schema=None):
        return self.rows

    def execute(self, sql):
        self.executed.append(sql)
        if self.explode:
            raise RuntimeError("catalog unavailable")
        return [(self.low, self.high)]


class _NonSqlSource(_FakeSource):
    """A source whose rows don't come from a SELECT -- MongoDB and the
    Excel/CSV source both read through fetch_batches_table instead."""

    def fetch_batches_table(self, table, batch_size=5000):
        return iter([])


def _matches(shard, value):
    """Evaluate a shard's predicate against one key value. The generated
    SQL is simple enough (`ID < 5`, `ID >= 5 AND ID < 9`, `ID >= 9`) to
    evaluate directly, which is the point -- this checks the actual
    semantics rather than the string."""
    if shard.where_sql is None:
        return True
    return eval(shard.where_sql.replace("ID", str(value)).replace(" AND ", " and "))


def _shards_matching(shards, value):
    return [s for s in shards if _matches(s, value)]


# --------------------------------------------------- the partition property

def test_every_value_in_range_belongs_to_exactly_one_shard():
    shards = sharding.build_shards(_table(), "ID", 1, 1000, 4)
    assert len(shards) == 4
    for value in range(1, 1001):
        assert len(_shards_matching(shards, value)) == 1, f"value {value} not covered exactly once"


def test_values_below_the_planned_minimum_are_still_covered():
    # MIN/MAX are read at planning time; rows can be inserted before the
    # observed minimum between planning and execution. The first shard is
    # open-ended below precisely so those aren't silently dropped.
    shards = sharding.build_shards(_table(), "ID", 100, 200, 4)
    for value in (99, 0, -1, -99999):
        assert len(_shards_matching(shards, value)) == 1


def test_values_above_the_planned_maximum_are_still_covered():
    shards = sharding.build_shards(_table(), "ID", 100, 200, 4)
    for value in (201, 1000, 10 ** 12):
        assert len(_shards_matching(shards, value)) == 1


def test_partition_holds_for_many_shard_counts_and_ranges():
    for low, high in ((1, 1000), (0, 7), (-500, 500), (1, 3)):
        for requested in (2, 3, 4, 8):
            shards = sharding.build_shards(_table(), "ID", low, high, requested)
            for value in range(low, high + 1):
                hits = _shards_matching(shards, value)
                assert len(hits) == 1, f"low={low} high={high} n={requested} value={value} hits={len(hits)}"


def test_no_shard_is_empty():
    # An empty shard is pure overhead: a connection, a query and a round
    # trip that return nothing.
    shards = sharding.build_shards(_table(), "ID", 1, 5, 8)
    for shard in shards:
        assert any(_matches(shard, v) for v in range(-10, 20)), f"{shard.where_sql} matches nothing"


# --------------------------------------------------------- shard identity

def test_shard_key_is_stable_and_includes_the_total():
    # Including the total means re-running with a different shard count
    # produces different keys, so a resumed run can't mistake a slice of
    # one partitioning for a slice of another.
    shards = sharding.build_shards(_table(), "ID", 1, 1000, 4)
    assert [s.key for s in shards] == ["1of4", "2of4", "3of4", "4of4"]
    other = sharding.build_shards(_table(), "ID", 1, 1000, 8)
    assert set(s.key for s in shards).isdisjoint(s.key for s in other)


def test_whole_table_shard_has_no_predicate():
    # The degenerate single shard must leave the migrator's SQL exactly as
    # it was before sharding existed -- not append a tautological WHERE.
    shards = sharding.plan_shards(_FakeSource(), _table(), max_shards=1)
    assert len(shards) == 1
    assert shards[0].where_sql is None
    assert shards[0].is_whole_table is True
    assert shards[0].label == "BIG"


def test_sharded_label_identifies_the_slice():
    shards = sharding.build_shards(_table(), "ID", 1, 1000, 4)
    assert shards[0].label == "BIG[1of4]"


# ------------------------------------------------------ when NOT to shard

def test_no_primary_key_falls_back_to_whole_table():
    shards = sharding.plan_shards(_FakeSource(), _table(with_pk=False), max_shards=4)
    assert len(shards) == 1
    assert "no single-column primary key" in shards[0].reason


def test_composite_primary_key_falls_back_to_whole_table():
    table = _table(pk_columns=["ID", "NAME"])
    shards = sharding.plan_shards(_FakeSource(), table, max_shards=4)
    assert len(shards) == 1
    assert "no single-column primary key" in shards[0].reason


def test_non_integer_primary_key_falls_back_to_whole_table():
    for pk_type in ("VARCHAR2(20)", "NUMBER(10,2)", "DATE", "RAW(16)"):
        shards = sharding.plan_shards(_FakeSource(), _table(pk_type=pk_type), max_shards=4)
        assert len(shards) == 1, f"{pk_type} should not be shardable"
        assert "not an integer type" in shards[0].reason


def test_integer_pivot_types_are_recognized():
    for pk_type in ("NUMBER", "NUMBER(10)", "NUMBER(10,0)", "INTEGER", "BIGINT", "SMALLINT"):
        shards = sharding.plan_shards(_FakeSource(), _table(pk_type=pk_type), max_shards=4)
        assert len(shards) == 4, f"{pk_type} should be shardable, got {shards[0].reason}"


def test_small_table_is_not_worth_sharding():
    source = _FakeSource(rows=500)
    shards = sharding.plan_shards(source, _table(), max_shards=4, min_rows=1000)
    assert len(shards) == 1
    assert "below the 1000-row sharding threshold" in shards[0].reason


def test_table_at_the_threshold_is_sharded():
    source = _FakeSource(rows=1000)
    shards = sharding.plan_shards(source, _table(), max_shards=4, min_rows=1000)
    assert len(shards) == 4


def test_source_without_sql_is_never_sharded():
    # MongoDB / Excel-CSV: there is no WHERE clause to attach a predicate
    # to in the first place.
    shards = sharding.plan_shards(_NonSqlSource(), _table(), max_shards=4)
    assert len(shards) == 1
    assert "without SQL" in shards[0].reason


def test_empty_table_falls_back_to_whole_table():
    class EmptySource(_FakeSource):
        def execute(self, sql):
            return [(None, None)]

    shards = sharding.plan_shards(EmptySource(), _table(), max_shards=4)
    assert len(shards) == 1
    assert "could not read MIN/MAX" in shards[0].reason


def test_unreadable_bounds_fall_back_to_whole_table():
    # A migration must never fail because shard *planning* failed --
    # falling back to the unsharded path is always correct, just slower.
    shards = sharding.plan_shards(_FakeSource(explode=True), _table(), max_shards=4)
    assert len(shards) == 1
    assert "could not read MIN/MAX" in shards[0].reason


def test_fractional_bounds_fall_back_to_whole_table():
    # A driver handing back a fractional value means the column wasn't
    # really an integer, whatever the declared type said.
    class FractionalSource(_FakeSource):
        def execute(self, sql):
            return [(1.5, 900.25)]

    shards = sharding.plan_shards(FractionalSource(), _table(), max_shards=4)
    assert len(shards) == 1


def test_decimal_bounds_that_are_whole_numbers_are_accepted():
    # An exact Decimal/float integer from a driver is fine -- it converts
    # without loss, which is the actual thing being checked.
    import decimal

    class DecimalSource(_FakeSource):
        def execute(self, sql):
            return [(decimal.Decimal("1"), decimal.Decimal("1000"))]

    shards = sharding.plan_shards(DecimalSource(), _table(), max_shards=4)
    assert len(shards) == 4


def test_narrow_key_range_is_not_split():
    shards = sharding.build_shards(_table(), "ID", 5, 5, 4)
    assert len(shards) == 1
    assert shards[0].is_whole_table


def test_shard_count_never_exceeds_distinct_key_values():
    shards = sharding.build_shards(_table(), "ID", 1, 3, 8)
    assert len(shards) <= 3
    for value in range(1, 4):
        assert len(_shards_matching(shards, value)) == 1


def test_strategy_none_disables_sharding():
    shards = sharding.plan_shards(
        _FakeSource(), _table(), max_shards=8, strategy=sharding.STRATEGY_NONE)
    assert len(shards) == 1
    assert shards[0].reason == "sharding disabled"


def test_row_count_can_be_supplied_instead_of_queried():
    class NoCountSource(_FakeSource):
        count_rows = None

    shards = sharding.plan_shards(
        NoCountSource(), _table(), max_shards=4, min_rows=1000, row_count=5000)
    assert len(shards) == 4


def test_unknown_row_count_does_not_block_sharding():
    # A source with no count_rows() shouldn't silently lose parallelism --
    # the threshold is an optimization, not a safety property.
    class NoCountSource(_FakeSource):
        count_rows = None

    shards = sharding.plan_shards(NoCountSource(), _table(), max_shards=4, min_rows=1000)
    assert len(shards) == 4


# ------------------------------------------------------------- probe SQL

def test_bounds_probe_reads_min_and_max_of_the_key_column():
    source = _FakeSource()
    sharding.plan_shards(source, _table(), max_shards=4)
    assert len(source.executed) == 1
    sql = source.executed[0]
    # Quoted: a primary key called `key` or `order` would otherwise make
    # the probe itself a syntax error. See tgdatabridge.db.source_sql.
    assert 'MIN("ID")' in sql and 'MAX("ID")' in sql
    assert '"HR"."BIG"' in sql


def test_reason_records_the_strategy_used():
    shards = sharding.plan_shards(_FakeSource(), _table(), max_shards=4)
    assert all(sharding.STRATEGY_PK_RANGE in s.reason for s in shards)
    assert all("ID" in s.reason for s in shards)
