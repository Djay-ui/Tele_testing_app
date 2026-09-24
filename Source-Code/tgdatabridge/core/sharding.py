"""Splitting one large table into several disjoint row ranges so it can be
migrated by several workers at once — SCALE.md section 1.2.

Why this exists
---------------
`migrate_schema(max_workers=N)` parallelizes *across* tables within an
FK-dependency wave, but within a table `migrate_table` is one sequential
`fetch_batches` loop. Real large databases are almost never evenly
distributed across their tables: typically two or three tables hold most
of the volume. So wall-clock time ends up bounded by the single largest
table on a single thread, no matter how high `max_workers` goes, and the
other workers sit idle once the small tables are done. Sharding is what
removes that ceiling.

The strategy: single-column integer primary key ranges
------------------------------------------------------
A shard is a SQL predicate appended to the migration's `SELECT`. For that
to be *correct* rather than merely fast, the set of predicates has to
partition the table exactly — every row matched by exactly one shard.
This module gets that guarantee by being narrow about what it will shard:

  - **A single-column primary key only.** Not "any numeric column": a PK
    is guaranteed non-NULL and, being indexed, gives the source a cheap
    range scan rather than N full table scans. A nullable column would
    silently drop every NULL row from every shard — a data-loss bug that
    a row-count check would catch only after hours of copying.
  - **An integer-typed key only.** Range arithmetic on floats and scaled
    decimals invites off-by-one and rounding gaps at the boundaries. A
    `NUMBER(10)` is fine; a `NUMBER(10,2)` is not.
  - **Open-ended at both ends.** The first shard is `key < b1` (not
    `key >= min AND key < b1`) and the last is `key >= bN`. The
    boundaries come from a `MIN`/`MAX` read at *planning* time, and rows
    can be inserted into the source between planning and execution — a
    closed range at either end would silently miss them. Unbounded ends
    mean the shards always cover the whole key space, whatever the data
    does afterwards.

Anything this module can't shard safely falls back to a single
whole-table shard with a recorded `reason`, which is exactly the
pre-sharding behaviour. That includes: no PK, a composite PK, a
non-integer PK, an empty table, a table below `min_rows`, and any source
whose rows don't come from a SQL query at all (MongoDB's array-unwind
child tables, the Excel/CSV source) — for those there is no `WHERE`
clause to attach a predicate to in the first place.

What is deliberately *not* here
-------------------------------
Oracle ROWID-range chunking (via `DBMS_PARALLEL_EXECUTE.CREATE_CHUNKS_BY_ROWID`
or a `DBA_EXTENTS` split) is the better strategy for an Oracle source when
it's available: it produces physically contiguous chunks and works on
tables with no usable numeric PK at all. It's deliberately left out for
now because it needs privileges this tool doesn't currently ask for, and
because it cannot be verified without a real Oracle instance — shipping
an unvalidated chunking strategy that silently produces overlapping or
gapped ranges would be worse than not having it. `plan_shards` takes a
`strategy` argument so it can be added as a second strategy later without
touching the migrator.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from tgdatabridge.core.schema_model import Table

# Below this many rows, sharding costs more than it saves: every shard
# is a separate connection, query and round trip, and the coordination
# overhead swamps the gain on a table that one worker finishes quickly.
DEFAULT_MIN_ROWS_TO_SHARD = 1_000_000

# Ceiling on how finely one table is split. More shards than workers is
# useful (it lets a fast worker pick up another range instead of idling
# at the end of a skewed table), but past a point each shard is too small
# to amortize its own setup.
DEFAULT_MAX_SHARDS_PER_TABLE = 8

STRATEGY_PK_RANGE = "pk_range"
STRATEGY_NONE = "none"

# "NUMBER(10)" / "INTEGER" / "NUMBER" -> integer; "NUMBER(10,2)" -> not.
# The pivot representation is Oracle-flavored (see schema_model.Column),
# so this checks the pivot type name, not any target engine's spelling.
_INTEGER_PIVOT_NAMES = {"NUMBER", "INTEGER", "INT", "SMALLINT", "BIGINT"}


@dataclass(frozen=True)
class Shard:
    """One disjoint slice of a table's rows.

    `where_sql` is None for the degenerate single shard covering the whole
    table, which is what every non-shardable table gets — that keeps the
    migrator's SQL byte-for-byte identical to the pre-sharding path rather
    than appending a tautological `WHERE 1=1`.
    """
    table_name: str
    index: int
    total: int
    where_sql: Optional[str] = None
    reason: str = ""

    @property
    def key(self) -> str:
        """Stable identifier for checkpointing. Includes `total` on
        purpose: re-running with a different shard count produces
        different keys, so a resumed run can't mistake a slice of one
        partitioning for a slice of another and skip rows that the new
        partitioning puts somewhere else."""
        return f"{self.index + 1}of{self.total}"

    @property
    def is_whole_table(self) -> bool:
        return self.total == 1 and self.where_sql is None

    @property
    def label(self) -> str:
        return self.table_name if self.is_whole_table else f"{self.table_name}[{self.key}]"


def _single_column_pk(table: Table) -> Optional[str]:
    for constraint in table.constraints:
        if constraint.kind == "PRIMARY KEY" and len(constraint.columns) == 1:
            return constraint.columns[0]
    return None


def _is_integer_pivot_type(data_type: str) -> bool:
    """True for a pivot type that holds whole numbers only. A declared
    scale (`NUMBER(10,2)`) disqualifies it — see this module's docstring
    on why range arithmetic is restricted to integers."""
    raw = (data_type or "").strip().upper()
    match = re.match(r"^([A-Z_ ]+?)\s*(?:\(([^)]*)\))?$", raw)
    if not match:
        return False
    name = match.group(1).strip()
    if name not in _INTEGER_PIVOT_NAMES:
        return False
    args = match.group(2)
    if args and "," in args:
        scale = args.split(",", 1)[1].strip()
        # NUMBER(10,0) is still an integer; NUMBER(10,2) is not.
        return scale in ("", "0")
    return True


def _column_type(table: Table, column_name: str) -> Optional[str]:
    for column in table.columns:
        if column.name.upper() == column_name.upper():
            return column.data_type
    return None


def _whole_table(table: Table, reason: str) -> List[Shard]:
    return [Shard(table_name=table.name, index=0, total=1, where_sql=None, reason=reason)]


def _boundaries(low: int, high: int, shard_count: int) -> List[int]:
    """Interior split points for [low, high]. Returns `shard_count - 1`
    strictly increasing values, each of which becomes the `>=` edge of the
    next shard."""
    span = high - low + 1
    step = span // shard_count
    return [low + step * i for i in range(1, shard_count)]


def build_shards(table: Table, key_column: str, low: int, high: int, shard_count: int,
                 quote=None) -> List[Shard]:
    """Turn an observed `[low, high]` key range into `shard_count`
    disjoint, exhaustive predicates over `key_column`.

    Exported separately from `plan_shards` so the range arithmetic can be
    tested directly, without a source connector to read MIN/MAX from --
    which is why `quote` defaults to leaving the name alone. plan_shards
    always passes the source engine's own quoting, because a primary key
    called `key` or `order` would otherwise make every shard predicate a
    syntax error (see tgdatabridge.db.source_sql).
    """
    if shard_count <= 1 or high <= low:
        return _whole_table(table, "key range too narrow to split")

    # Never create more shards than there are distinct key values -- that
    # would produce empty shards whose only effect is overhead.
    shard_count = min(shard_count, high - low + 1)
    cuts = _boundaries(low, high, shard_count)
    # Degenerate spans can repeat a boundary; collapsing duplicates keeps
    # every shard non-empty.
    cuts = sorted(set(cuts))
    if not cuts:
        return _whole_table(table, "key range too narrow to split")

    quoted = quote(key_column) if quote else key_column
    shards: List[Shard] = []
    total = len(cuts) + 1
    for i in range(total):
        if i == 0:
            where = f"{quoted} < {cuts[0]}"
        elif i == total - 1:
            where = f"{quoted} >= {cuts[-1]}"
        else:
            where = f"{quoted} >= {cuts[i - 1]} AND {quoted} < {cuts[i]}"
        shards.append(Shard(
            table_name=table.name, index=i, total=total, where_sql=where,
            reason=f"{STRATEGY_PK_RANGE} on {key_column}",
        ))
    return shards


def plan_shards(
    source,
    table: Table,
    max_shards: int = DEFAULT_MAX_SHARDS_PER_TABLE,
    min_rows: int = DEFAULT_MIN_ROWS_TO_SHARD,
    strategy: str = STRATEGY_PK_RANGE,
    row_count: Optional[int] = None,
) -> List[Shard]:
    """Decide how (and whether) to split `table` for parallel migration.

    Always returns at least one shard. A single whole-table shard means
    "migrate this exactly the way it was migrated before sharding
    existed", and every rejection path below leads there with a `reason`
    recorded for the run log — a table silently not being sharded is the
    kind of thing that turns into a puzzling slow migration otherwise.

    `row_count`, if given, is used instead of querying the source (the
    caller often already has it from dry-run planning).
    """
    if strategy == STRATEGY_NONE or max_shards <= 1:
        return _whole_table(table, "sharding disabled")

    # A source whose rows don't come from a SQL SELECT has no WHERE clause
    # to hang a predicate on: MongoDB (including its synthesized
    # array-unwind child tables) and the Excel/CSV source both read rows
    # through fetch_batches_table instead. See migrator.migrate_table.
    if hasattr(source, "fetch_batches_table"):
        return _whole_table(table, "source reads rows without SQL (no WHERE clause to shard on)")

    key_column = _single_column_pk(table)
    if key_column is None:
        return _whole_table(table, "no single-column primary key to shard on")

    key_type = _column_type(table, key_column)
    if key_type is None or not _is_integer_pivot_type(key_type):
        return _whole_table(
            table, f"primary key {key_column} is {key_type or 'of unknown type'}, not an integer type")

    if row_count is None:
        row_count = _count_rows(source, table)
    if row_count is not None and row_count < min_rows:
        return _whole_table(table, f"{row_count} rows is below the {min_rows}-row sharding threshold")

    bounds = _key_bounds(source, table, key_column)
    if bounds is None:
        return _whole_table(table, f"could not read MIN/MAX of {key_column}")
    low, high = bounds

    from tgdatabridge.db.source_sql import quoter_for

    return build_shards(table, key_column, low, high, max_shards,
                        quote=quoter_for(source))


def _count_rows(source, table: Table) -> Optional[int]:
    counter = getattr(source, "count_rows", None)
    if counter is None:
        return None
    try:
        return counter(table.name, schema=table.schema)
    except Exception:  # noqa: BLE001 - an unavailable count must never block a migration
        return None


def _key_bounds(source, table: Table, key_column: str):
    """`(min, max)` of the key column, or None if it can't be read or the
    table is empty. Both values must be whole numbers: a driver that
    hands back a Decimal for an integer column is fine (it converts
    exactly), but anything with a fractional part means the column wasn't
    really an integer and sharding on it isn't safe."""
    from tgdatabridge.db.source_sql import qualified_table, quoter_for

    quote = quoter_for(source)
    column = quote(key_column)
    sql = (f"SELECT MIN({column}), MAX({column}) FROM "
           f"{qualified_table(source, table.name, table.schema)}")
    try:
        rows = list(source.execute(sql))
    except Exception:  # noqa: BLE001 - fall back to whole-table rather than failing the migration
        return None
    if not rows or not rows[0]:
        return None
    low_raw, high_raw = rows[0][0], rows[0][1]
    if low_raw is None or high_raw is None:
        return None  # empty table
    try:
        low, high = int(low_raw), int(high_raw)
    except (TypeError, ValueError):
        return None
    if low != low_raw or high != high_raw:
        return None  # fractional -- not really an integer key
    return low, high
