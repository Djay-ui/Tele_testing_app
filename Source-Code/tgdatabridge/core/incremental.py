"""Bringing the target up to date after the first full migration.

A migration is almost never a single event. The bulk load runs, and then
the source keeps working: rows are added, rows are edited, rows are
deleted. Until now this tool could only re-run the whole thing, which for
a 6.5 million row schema means hours to move a few hundred changed rows.

This makes the target a mirror of the source again, moving only what
actually differs.

WHAT COUNTS AS A CHANGE
-----------------------
All three: rows added, rows whose values differ, and rows deleted at the
source. The last one is the reason this is not simply "copy anything with
a recent timestamp" -- a deleted row leaves nothing behind to have a
timestamp, so the only way to find it is to ask the target what it holds
and check each key against the source.

HOW A CHANGED ROW IS FOUND
--------------------------
Per table, whichever is cheaper and safe:

**timestamp** -- the table has a column like `updated_at` / `modified` /
`last_updated` with a date-time type. The sync reads only rows where that
column is newer than the last run's high-water mark, so a 250,000 row
table with 40 edits reads 40 rows.

**compare** -- no such column. Every source row is read and hashed, and
compared against the target's own row for that key. Slower, but it
notices an edit the application forgot to stamp, which the timestamp
strategy by definition cannot.

The strategy is chosen per table and reported, so a table quietly falling
back to the expensive path is visible rather than surprising.

NO MERGE-JOIN, ON PURPOSE
-------------------------
The obvious implementation streams both sides ordered by primary key and
walks them together. It is wrong across engines: MySQL's default
collation sorts `'a'` and `'A'` together, PostgreSQL's does not, and a
merge-join fed two differently-ordered streams silently reports the
entire table as inserts *and* deletes. So instead the source is read in
batches and the target is asked about exactly those keys
(`WHERE (k) IN (...)`), and then the reverse for deletes. Two directions,
no ordering assumption anywhere, exact on any collation.

WHAT IT REFUSES
---------------
A table with no primary key. There is no way to say which target row a
changed source row corresponds to, so an "update" would have to be a
delete-everything-and-reinsert. That is a full reload wearing an
incremental costume, and it is not what anyone pressing "sync" expects.
Those tables are skipped by name, with the reason.
"""
from __future__ import annotations

import datetime
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from tgdatabridge.core.schema_model import Table
from tgdatabridge.core.validation import _canonical, row_checksum
from tgdatabridge.db import target_sql
from tgdatabridge.db.source_sql import qualified_table, quote_columns, quoter_for

#: How a table's changed rows are found.
STRATEGY_TIMESTAMP = "timestamp"
STRATEGY_COMPARE = "compare"
STRATEGY_SKIP = "skip"

#: Column names that conventionally carry "when this row last changed".
#: Ordered: the earlier a pattern appears the more it is trusted, so a
#: table with both `updated_at` and `created_at` picks the right one.
_CHANGE_COLUMN_PATTERNS = [
    re.compile(r"^(last_)?updated?(_at|_on|_date|_time|_ts)?$", re.IGNORECASE),
    re.compile(r"^(last_)?modified?(_at|_on|_date|_time|_ts)?$", re.IGNORECASE),
    re.compile(r"^(date|time)_(updated|modified|changed)$", re.IGNORECASE),
    re.compile(r"^(last_)?changed(_at|_on)?$", re.IGNORECASE),
    re.compile(r"^row_version$|^rowversion$|^version_ts$", re.IGNORECASE),
]

#: Only a genuine date/time column is trusted as a high-water mark. A
#: `updated_by` VARCHAR matches the name patterns above and would make
#: every comparison a string comparison -- which succeeds, returns the
#: wrong rows, and looks like it worked.
_TEMPORAL_TYPE_RE = re.compile(
    r"\b(DATE|TIME|TIMESTAMP|DATETIME|SMALLDATETIME|DATETIME2|DATETIMEOFFSET)\b",
    re.IGNORECASE)


def _is_temporal(data_type: str) -> bool:
    return bool(_TEMPORAL_TYPE_RE.search(data_type or ""))


def primary_key_columns(table: Table) -> List[str]:
    for constraint in table.constraints:
        if constraint.kind == "PRIMARY KEY" and constraint.columns:
            return list(constraint.columns)
    # Some sources report the primary key only as a unique index; a unique
    # key identifies a row just as well for this purpose.
    for constraint in table.constraints:
        if constraint.kind == "UNIQUE" and constraint.columns:
            return list(constraint.columns)
    return []


def change_column_for(table: Table) -> Optional[str]:
    """The column to use as a high-water mark, or None."""
    by_name = {c.name.lower(): c for c in table.columns}
    for pattern in _CHANGE_COLUMN_PATTERNS:
        for column in table.columns:
            if pattern.match(column.name) and _is_temporal(column.data_type):
                return column.name
    # `updated`/`modified` spelled some other way but still obviously
    # temporal and obviously about change.
    for column in table.columns:
        lowered = column.name.lower()
        if _is_temporal(column.data_type) and (
                "updat" in lowered or "modif" in lowered or "chang" in lowered):
            return by_name[lowered].name
    return None


@dataclass
class TableSyncPlan:
    table: Table
    strategy: str
    key_columns: List[str] = field(default_factory=list)
    change_column: Optional[str] = None
    reason: str = ""

    @property
    def name(self) -> str:
        return self.table.name


def plan_table(table: Table) -> TableSyncPlan:
    keys = primary_key_columns(table)
    if not keys:
        return TableSyncPlan(
            table=table, strategy=STRATEGY_SKIP,
            reason=(f"{table.name} has no primary key or unique constraint, so there is no "
                    f"way to tell which target row a changed source row belongs to. "
                    f"Add a key, or re-migrate this table in full."))
    change = change_column_for(table)
    if change:
        return TableSyncPlan(
            table=table, strategy=STRATEGY_TIMESTAMP, key_columns=keys, change_column=change,
            reason=f"reads only rows whose {change} is newer than the last run")
    return TableSyncPlan(
        table=table, strategy=STRATEGY_COMPARE, key_columns=keys,
        reason="no date/time column recording when a row changed, so every row is compared")


def plan_schema(tables: Sequence[Table]) -> List[TableSyncPlan]:
    return [plan_table(table) for table in tables]


@dataclass
class TableSyncResult:
    table_name: str
    strategy: str
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    source_rows_read: int = 0
    target_keys_read: int = 0
    watermark: Optional[str] = None
    elapsed_seconds: float = 0.0
    skipped: bool = False
    reason: str = ""
    error: Optional[str] = None

    @property
    def changed(self) -> int:
        return self.inserted + self.updated + self.deleted

    def summary(self) -> str:
        if self.skipped:
            return f"{self.table_name}: skipped -- {self.reason}"
        if self.error:
            return f"{self.table_name}: FAILED -- {self.error}"
        if not self.changed:
            return f"{self.table_name}: no changes ({self.strategy})"
        return (f"{self.table_name}: +{self.inserted} inserted, "
                f"~{self.updated} updated, -{self.deleted} deleted ({self.strategy})")


@dataclass
class SyncReport:
    results: List[TableSyncResult] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def inserted(self) -> int:
        return sum(r.inserted for r in self.results)

    @property
    def updated(self) -> int:
        return sum(r.updated for r in self.results)

    @property
    def deleted(self) -> int:
        return sum(r.deleted for r in self.results)

    @property
    def changed(self) -> int:
        return self.inserted + self.updated + self.deleted

    @property
    def failed(self) -> List[str]:
        return [r.table_name for r in self.results if r.error]

    @property
    def skipped(self) -> List[str]:
        return [r.table_name for r in self.results if r.skipped]

    def headline(self) -> str:
        if not self.results:
            return "Nothing to sync."
        if not self.changed and not self.failed:
            return (f"Already in sync -- {len(self.results)} table(s) checked in "
                    f"{self.elapsed_seconds:.1f}s, nothing had changed.")
        return (f"{self.changed:,} row(s) synced in {self.elapsed_seconds:.1f}s: "
                f"{self.inserted:,} inserted, {self.updated:,} updated, "
                f"{self.deleted:,} deleted.")


# --------------------------------------------------------------- helpers


def _hash_row(row: Sequence) -> int:
    """The same normalisation post-migration validation uses, so a row
    this reports as changed is a row that check would also flag -- and,
    more importantly, a row that differs only by the mappings this tool
    deliberately introduces (a date widened to midnight, a CHAR blank-
    padded on read) is NOT reported as changed on every single run."""
    return row_checksum(tuple(row))


def _key_of(row: Sequence, indexes: Sequence[int]) -> tuple:
    """The row's key, folded the same way its values are.

    This matters more than it looks. A `CHAR(8)` primary key comes back
    from PostgreSQL blank-padded to 8 and from MySQL stripped, so the very
    same key is `'AA      '` on one side and `'AA'` on the other. Compared
    raw, every source row looks new *and* every target row looks deleted:
    a sync straight after a successful migration would report the whole
    table as changed, delete every row, and re-insert it. Folding with the
    same `_canonical` post-migration validation uses keeps the two sides
    talking about the same key.

    Only the in-memory comparison is folded. The values bound into the
    SELECT and DELETE stay exactly as their own side reported them, which
    is what the server needs to find the row again.
    """
    return tuple(_canonical(row[i]) for i in indexes)


def _as_watermark(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat(sep=" ")
    return str(value)


def _chunks(items: Sequence, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


# ------------------------------------------------------------ one table


def sync_table(
    source,
    target,
    plan: TableSyncPlan,
    watermark: Optional[str] = None,
    batch_size: int = 2000,
    detect_deletes: bool = True,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> TableSyncResult:
    """Bring one table's target rows into line with its source rows."""
    started = time.monotonic()
    result = TableSyncResult(table_name=plan.table.name, strategy=plan.strategy,
                             reason=plan.reason)
    if plan.strategy == STRATEGY_SKIP:
        result.skipped = True
        return result

    table = plan.table
    columns = [c.name for c in table.columns]
    key_indexes = [columns.index(k) for k in plan.key_columns if k in columns]
    if len(key_indexes) != len(plan.key_columns):
        result.skipped = True
        result.reason = (f"{table.name}: the key column(s) "
                         f"{', '.join(plan.key_columns)} are not among the columns this "
                         f"tool read for the table, so it cannot be synced by key.")
        return result

    upsert = target_sql.upsert_sql(target, table.name, columns, plan.key_columns)
    if upsert is None:
        result.skipped = True
        result.reason = (f"{table.name}: this target engine has no merge-on-key statement, "
                         f"so an incremental sync cannot update rows in place.")
        return result

    try:
        highest = _sync_forward(source, target, plan, columns, key_indexes,
                               upsert, watermark, batch_size, result, progress_cb)
        result.watermark = highest or watermark
        if detect_deletes:
            _sync_deletes(source, target, plan, columns, batch_size, result, progress_cb)
    except Exception as exc:  # noqa: BLE001 - one table's failure must not end the run
        result.error = str(exc).splitlines()[0]
    result.elapsed_seconds = time.monotonic() - started
    return result


def _select_source(source, plan: TableSyncPlan, columns: Sequence[str],
                   watermark: Optional[str]) -> str:
    quote = quoter_for(source)
    sql = (f"SELECT {quote_columns(source, columns)} FROM "
           f"{qualified_table(source, plan.table.name, plan.table.schema)}")
    if plan.strategy == STRATEGY_TIMESTAMP and watermark:
        # Inclusive on purpose. A row written in the same second as the
        # last run's high-water mark would be missed by a strict `>`, and
        # re-copying a handful of rows is free -- the upsert makes it a
        # no-op -- while missing one is silent data loss.
        sql += f" WHERE {quote(plan.change_column)} >= '{watermark}'"
    return sql


def _sync_forward(source, target, plan, columns, key_indexes, upsert,
                  watermark, batch_size, result, progress_cb) -> Optional[str]:
    """Everything the source has that the target does not, or has
    differently."""
    change_index = (columns.index(plan.change_column)
                    if plan.change_column in columns else None)
    highest = watermark
    key_columns = plan.key_columns
    sql = _select_source(source, plan, columns, watermark)

    # fetch_batches yields (column_names, rows) -- the same contract
    # migrator.migrate_table consumes. Reading it as a bare list of rows
    # makes the column-name header look like the first data row, which
    # the target then rejects with "invalid input syntax for type date:
    # 'joined_on'" -- the column's own name, bound as a value.
    for _batch_columns, batch in source.fetch_batches(sql, batch_size=batch_size):
        if not batch:
            continue
        result.source_rows_read += len(batch)
        keys = [_key_of(row, key_indexes) for row in batch]

        # One question per batch rather than per row: what does the target
        # already hold for these keys?
        existing: Dict[tuple, tuple] = {}
        select = target_sql.select_by_keys_sql(
            target, plan.table.name, columns, key_columns, len(keys))
        for row in target_sql.execute_with(target, select, target_sql.flatten_keys(keys)):
            existing[_key_of(row, key_indexes)] = tuple(row)

        to_write = []
        for row, key in zip(batch, keys):
            found = existing.get(key)
            if found is None:
                result.inserted += 1
                to_write.append(row)
            elif _hash_row(found) != _hash_row(row):
                result.updated += 1
                to_write.append(row)
            # else: identical, nothing to do -- the common case on a
            # timestamp table whose watermark is inclusive.
        if to_write:
            target_sql.execute_many(target, upsert, to_write)

        if change_index is not None:
            for row in batch:
                candidate = _as_watermark(row[change_index])
                if candidate and (highest is None or candidate > highest):
                    highest = candidate
        if progress_cb:
            progress_cb(plan.table.name, result.source_rows_read)
    return highest


def _sync_deletes(source, target, plan, columns, batch_size, result, progress_cb) -> None:
    """Rows the target still has that the source no longer does.

    Runs whatever strategy found the changed rows: a deleted row leaves no
    timestamp behind, so there is nothing cheaper than asking the source
    whether each key it holds still exists.
    """
    key_columns = plan.key_columns
    quote = quoter_for(source)
    target_keys_sql = (
        f"SELECT {', '.join(target_sql.identifier(target, c) for c in key_columns)} "
        f"FROM {target_sql.qualified(target, plan.table.name)}")
    source_table = qualified_table(source, plan.table.name, plan.table.schema)
    key_list = ", ".join(quote(c) for c in key_columns)
    left = key_list if len(key_columns) == 1 else f"({key_list})"

    cursor = target._conn.cursor()
    try:
        cursor.execute(target_keys_sql)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            keys = [tuple(r) for r in rows]
            result.target_keys_read += len(keys)

            markers = ", ".join(
                "(" + ", ".join(["%s"] * len(key_columns)) + ")"
                for _ in keys) if len(key_columns) > 1 else \
                ", ".join(["%s"] * len(keys))
            check = (f"SELECT {key_list} FROM {source_table} "
                     f"WHERE {left} IN ({markers})")
            still_there = set()
            source_cursor = source._conn.cursor()
            try:
                source_cursor.execute(
                    _to_source_paramstyle(source, check),
                    tuple(target_sql.flatten_keys(keys)))
                for row in source_cursor.fetchall():
                    # Folded, for the same reason _key_of folds: the two
                    # engines disagree about trailing spaces on a CHAR key,
                    # and comparing raw would mark every row deleted.
                    still_there.add(tuple(_canonical(v) for v in row))
            finally:
                source_cursor.close()

            gone = [key for key in keys
                    if tuple(_canonical(v) for v in key) not in still_there]
            for chunk in _chunks(gone, batch_size):
                delete = target_sql.delete_by_keys_sql(
                    target, plan.table.name, key_columns, len(chunk))
                target_sql.execute_with(
                    target, delete, target_sql.flatten_keys(chunk))
                result.deleted += len(chunk)
            if progress_cb:
                progress_cb(plan.table.name, result.target_keys_read)
    finally:
        cursor.close()


def _to_source_paramstyle(source, sql: str) -> str:
    """The delete check is built with `%s`; rewrite it for a source whose
    driver binds differently."""
    name = type(source).__name__.lower()
    if "sqlserver" in name or "db2" in name:
        return sql.replace("%s", "?")
    if "oracle" in name:
        out, position = [], 1
        for piece in sql.split("%s"):
            out.append(piece)
            out.append(f":{position}")
            position += 1
        return "".join(out[:-1])
    return sql


# ---------------------------------------------------------- whole schema


def sync_schema(
    source,
    target,
    tables: Sequence[Table],
    watermarks: Optional[Dict[str, str]] = None,
    batch_size: int = 2000,
    detect_deletes: bool = True,
    progress_cb: Optional[Callable[[str, int], None]] = None,
    on_table_done: Optional[Callable[[TableSyncResult], None]] = None,
) -> SyncReport:
    """Sync every table, in the order a full migration would use so a new
    row and the parent it references arrive in the right sequence."""
    from tgdatabridge.core.migrator import order_tables_by_dependency

    started = time.monotonic()
    report = SyncReport()
    watermarks = dict(watermarks or {})

    for table in order_tables_by_dependency(list(tables)):
        plan = plan_table(table)
        result = sync_table(
            source, target, plan, watermark=watermarks.get(table.name),
            batch_size=batch_size, detect_deletes=detect_deletes,
            progress_cb=progress_cb)
        if result.watermark:
            watermarks[table.name] = result.watermark
        report.results.append(result)
        if on_table_done:
            on_table_done(result)

    report.elapsed_seconds = time.monotonic() - started
    return report


def watermarks_from(report: SyncReport, previous: Optional[Dict[str, str]] = None
                    ) -> Dict[str, str]:
    """The high-water marks to carry into the next run.

    A table that failed keeps its previous mark rather than the one this
    run reached: advancing past rows that were read but not written would
    make those changes invisible to every future sync.
    """
    marks = dict(previous or {})
    for result in report.results:
        if result.error or result.skipped:
            continue
        if result.watermark:
            marks[result.table_name] = result.watermark
    return marks
