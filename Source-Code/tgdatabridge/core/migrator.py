"""
Basic data migration: streams rows out of the source in batches and
inserts them into the already-created target table. This is intentionally
simple (single-threaded, no CDC/ongoing-replication) — a starting point
for one-time cutover migrations of small-to-medium tables, not a
replacement for a full replication tool on very large datasets.

`source` was Oracle-only when this module was first written; it now
accepts any connector with the same duck-typed `fetch_batches(sql,
batch_size)` streaming-fetch method OracleConnector and MySQLConnector
both implement (and every future source connector is expected to as
well) — this module itself has no source-engine-specific logic at all,
the SQL it builds (`SELECT cols FROM schema.table`) is portable as-is.

MongoConnector is the one exception: a SQL string can't express "unwind
this array field out of that parent collection" for one of
mongo_source_introspector's synthesized child tables (see
MongoConnector.fetch_batches_table's own docstring for why), so
migrate_table() below prefers a *second*, differently-shaped duck-typed
method -- `fetch_batches_table(table, batch_size)`, taking the whole
Table object instead of a SQL string -- whenever the source connector
defines one. This is purely additive: every other connector (Oracle/
MySQL/PostgreSQL/SQL Server/Db2) has no `fetch_batches_table` attribute
at all, so `hasattr(...)` is False for all of them and they fall straight
through to the exact original SQL-string `fetch_batches` path, unchanged.

Reliability features (see ENTERPRISE_READINESS.md section 2 for the
motivation behind each):

  - Post-migration validation: once a table's rows are streamed to the
    target, an independent re-query against the target (row count, plus a
    checksum for tables at or under `checksum_max_rows`) catches a target
    driver that silently drops/truncates rows on write -- something
    `rows_copied` alone can't catch, since it only counts what was *sent*.
    See tgdatabridge.core.validation. Controlled by `validate`/`checksum_max_rows`
    below; set `validate=False` to skip it entirely (e.g. a target
    connector too old to have count_rows()).
  - Checkpoint/resume: pass a `tgdatabridge.utils.app_storage.MigrationCheckpoint`
    as `checkpoint` and migrate_schema will skip any table already marked
    "done" on it, and resume a partially-migrated table by re-reading the
    source from the start but only re-inserting the batches not already
    known to have reached the target (see migrate_table's own comment on
    why only the *write* side of a resume is skippable, not the read
    side). Two callbacks let the caller persist it incrementally without
    this module ever touching disk itself: `on_checkpoint_update` fires
    after every batch and means "progress advanced" -- the caller may
    coalesce these freely -- while `on_checkpoint_flush` fires only on a
    terminal transition (a table or shard reaching done or failed) and
    means "this state must not be lost", since that's what a resume
    actually reads. `on_checkpoint_flush` defaults to
    `on_checkpoint_update`, so a caller that passes only the latter gets
    the original write-after-every-batch behavior unchanged. See
    app_storage.CheckpointWriter, which implements exactly this split,
    and SCALE.md section 1.5 for why per-batch writes became a problem
    once the COPY path and sharding raised throughput.
  - Retry with backoff: pass a `tgdatabridge.core.retry.RetryPolicy` as
    `retry_policy` and every `target.insert_batch()` call is retried
    through `tgdatabridge.core.retry.retry_call` instead of failing the whole
    table on the first transient error. See retry.py's own docstring for
    the correctness trade-off this implies under autocommit targets. A
    retry that's actually about a torn-down connection (see
    `_reconnect_callback`) also closes and re-opens the target's
    connection before the retried call, or it would just fail the same
    way every time. A *source* connection torn down mid-fetch (Oracle's
    DPY-4011, hit on a real migration streaming a LOB-heavy table) gets
    the same treatment, but only when `checkpoint` is also given: the
    whole read is retried from scratch on a reconnected source, relying
    on the checkpoint's own batch-skip bookkeeping (updated as this same
    call proceeds, not just across separate calls) to avoid re-inserting
    what already reached the target before the drop. Without a
    checkpoint this table still fails outright, exactly as before, but
    the source connection is reconnected before returning regardless --
    otherwise every table migrated after this one in the same run
    inherits the same dead connection and fails instantly with Oracle's
    "DPY-1001: not connected to database", which is what actually
    happened to 17 tables in a row on that same real migration.
  - Dry-run / plan mode: `plan_table`/`plan_schema` below make exactly the
    read-only checks needed to answer "would this table's migration likely
    succeed, and how much data would move" (source row count, target
    existence/row count) without writing anything to either side --
    unrelated to (and safe to call before, or without ever calling)
    migrate_table/migrate_schema.

Scale features (see ENTERPRISE_READINESS.md section 4 for the motivation
behind each):

  - Parallel table migration: pass `max_workers > 1` (plus `source_factory`/
    `target_factory` -- see migrate_schema's own docstring for why those
    are required, not the already-connected `source`/`target` objects) and
    tables are migrated several at a time via a bounded thread pool,
    instead of one at a time. Parallelism is bounded *within* each
    FK-dependency "wave" from order_tables_by_dependency_waves, never
    across waves -- a child table's wave never starts until every table in
    its parent's wave has finished, so this changes nothing about
    correctness under live FK constraints, only how much of one wave's own
    tables move at once. `max_workers=1` (the default) is unchanged,
    single-threaded, byte-for-byte identical behavior to before this
    feature existed -- every existing caller (the GUI, every test in this
    file predating this feature) needs no changes at all.
  - LOB-aware batch sizing: a table with a CLOB/NCLOB/BLOB/LONG/LONG RAW
    column (per Column.data_type) automatically uses a smaller row batch
    size (`lob_batch_size`, default 2000) instead of the usual `batch_size`
    (default 5000) -- holding 5000 rows' worth of potentially-large LOB
    values in memory at once, in one fetchmany()/insert_batch() call, is
    the actual scaling risk for these columns, independent of how any one
    LOB value itself is read (see oracle_connector.OracleConnector.
    fetch_batches's own docstring for the complementary per-LOB-value fix).
    The default was 200 until a user explicitly asked for LOB migration
    to move faster; 2000 was chosen as a middle ground between that and
    the plain `batch_size` -- fewer, bigger round trips per table without
    dropping the original memory-safety margin to zero. Set
    `lob_batch_size=None` to disable this narrowing and always use the
    plain `batch_size`, or pass a larger explicit value (5000, say) for a
    table with many small CLOB values where even more caution isn't
    warranted.
  - LOB-aware sharding threshold: intra-table sharding (below) normally
    only splits a table with at least `min_rows_to_shard` (default
    1,000,000) rows -- far too high a bar for a LOB table, whose cost is
    dominated by data *volume*, not row count. `lob_min_rows_to_shard`
    (default 20,000) is that same threshold, applied only to a table
    _table_has_lob_columns flags, so a 90,000-row document table gets
    split across workers the same way a 5,000,000-row plain table already
    would, instead of tying up one worker/one connection for the entire
    run while every other worker sits idle. See migrate_schema's own
    docstring for the real migration this came from.
  - Pause/resume mid-run: pass a `threading.Event` as `pause_event`,
    already `.set()`, and clearing it from another thread (the GUI's
    Pause button) blocks every worker between batches -- mid-batch writes
    always finish first, so pausing can never leave a batch half-written
    -- until it's `.set()` again. This is a live pause of the *same* run
    (connections stay open and idle throughout, nothing is reconnected or
    replanned), a lighter-weight tool than stopping and relying on
    checkpoint/resume for the case where all a person wants is "hold on a
    moment" -- e.g. to let a noisy neighbor workload on the source/target
    quiet down before the migration keeps consuming its I/O. One shared
    Event covers every worker thread when `max_workers > 1`, since
    `threading.Event` is itself thread-safe. None (the default) never
    pauses, exactly as before this parameter existed.
  - LOB checksum skipped by design: a table with a LOB column also skips
    the row-checksum validation entirely (row-count validation still
    runs) -- see `skip_checksum` inside migrate_table for the two
    reasons: LOB serialization isn't guaranteed byte-identical between
    source and target drivers even when the content matches (a real
    migration hit exactly this, reported as "row count matched but
    checksum did not" on tables that had in fact migrated correctly),
    and hashing large values twice (once per side) is real, avoidable
    CPU cost on exactly the tables slow enough for it to matter.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from tgdatabridge.core import sharding
from tgdatabridge.core.retry import RetryPolicy, is_transient, retry_call
from tgdatabridge.core.schema_model import Table
from tgdatabridge.core.target_shape import check_table_shape
from tgdatabridge.core.validation import ValidationResult, row_checksum, validate_table
from tgdatabridge.db.pool import ConnectionPool

# Pivot-representation type names (Column.data_type -- see schema_model.py's
# own comment on what "pivot representation" means) that hold potentially-
# large values, used by _table_has_lob_columns for LOB-aware batch sizing.
# Matched against the type name with any "(...)" size/precision suffix
# stripped, so e.g. "RAW(2000)" (not a LOB -- Oracle caps RAW at 2000 bytes)
# doesn't false-positive while "LONG RAW" (unbounded, genuinely LOB-like)
# does.
_LOB_TYPE_NAMES = {"CLOB", "NCLOB", "BLOB", "LONG", "LONG RAW"}


def _table_has_lob_columns(table: Table) -> bool:
    for col in table.columns:
        base = col.data_type.split("(", 1)[0].strip().upper()
        if base in _LOB_TYPE_NAMES:
            return True
    return False


def _effective_batch_size(table: Table, batch_size: int, lob_batch_size: Optional[int]) -> int:
    if lob_batch_size is not None and _table_has_lob_columns(table):
        return min(batch_size, lob_batch_size)
    return batch_size


def _resume_order_columns(table: Table) -> Optional[List[str]]:
    """The columns migrate_table's SELECT should ORDER BY, so that
    re-running the exact same query on a resumed migration reads rows
    back in the exact same order -- which the batch-skip resume logic
    below (see its own comment starting "Resuming:") silently assumes,
    but which nothing guarantees without an explicit ORDER BY. A bare
    `SELECT cols FROM table` has no defined row order on any of these
    engines: Oracle in particular is free to return a full table scan's
    rows in a different order on every execution (parallel query, direct
    path reads, or just a different execution plan after the table
    changed), and did exactly that on a real production migration this
    was found from -- a table interrupted mid-resume ended up with fewer
    rows on the target than were sent (some batches' rows were, in the
    second run's different ordering, wrongly classified as "already
    written" and skipped on read, so they were never re-inserted either),
    while a sibling table ended up with *more* than were sent (the
    reverse mistake: rows genuinely already on the target were read into
    a batch this run didn't recognize as already-written, and duplicated).

    A table's primary key is the only column set this function trusts:
    it's the one this tool already knows is unique per row (a UNIQUE
    index could still permit NULLs, which sort unpredictably against each
    other), so ordering by every PK column, in the order the constraint
    lists them, is a total, stable, repeatable order over the table's
    rows -- unaffected by inserts/updates elsewhere, unlike an ORDER BY
    on a rowid/ctid-style physical position. Returns None (no ORDER BY
    added, unchanged pre-existing behavior) for a table with no primary
    key at all -- not common among the tables large enough for a resume
    to matter, but there is no column set this function can invent for
    one that is still guaranteed unique."""
    for constraint in table.constraints:
        if constraint.kind == "PRIMARY KEY" and constraint.columns:
            return constraint.columns
    return None


def _reconnect_callback(target) -> Optional[Callable[[], None]]:
    """A zero-arg callable that closes and re-opens `target`'s
    connection, for retry.retry_call's `reconnect` parameter -- or None
    when `target` doesn't duck-type both `close()` and `connect()` (this
    tool's own lighter test doubles, and any future connector that
    manages its connection some other way). See retry.py's own docstring
    for why this exists: retrying target.insert_batch() after a
    connection the network or database actually tore down otherwise
    fails identically every single time, since nothing about a plain
    retry re-establishes the connection itself."""
    if not (hasattr(target, "close") and hasattr(target, "connect")):
        return None

    def reconnect() -> None:
        target.close()
        target.connect()

    return reconnect


@dataclass
class MigrationResult:
    table_name: str
    rows_copied: int = 0
    succeeded: bool = False
    error: Optional[str] = None
    skipped: bool = False  # True when a checkpoint already marked this table "done" on a previous run
    validation: Optional[ValidationResult] = None

    # Sharding metadata (see tgdatabridge.core.sharding). `shard_key` is None for
    # an unsharded table, so every pre-sharding caller sees exactly the
    # result shape it always did. `checksum`/`columns` are what let
    # migrate_schema combine several shards' results into one table-level
    # validation: the running checksum is XOR-combined and therefore
    # order-independent, so shard checksums compose by XOR for free.
    shard_key: Optional[str] = None
    shard_count: int = 1
    # None means "not comparable" rather than zero -- reached when a
    # resumed run reuses a shard finished by a build whose row_checksum
    # differed (app_storage.load_checkpoint sets checksums_stale). It
    # propagates through the shard combine below into
    # validate_table(expected_checksum=None), which skips the checksum
    # comparison and still checks row counts.
    checksum: Optional[int] = 0
    columns: Optional[List[str]] = None


@dataclass
class MigrationReport:
    results: List[MigrationResult] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(r.rows_copied for r in self.results)

    @property
    def failed_tables(self) -> List[str]:
        return [r.table_name for r in self.results if not r.succeeded]

    @property
    def unvalidated_tables(self) -> List[str]:
        """Tables that migrate_table reported as succeeded, but whose
        post-migration validation either found a mismatch or couldn't run
        at all (e.g. the target connector has no count_rows()). Kept
        separate from failed_tables: the migration call itself didn't
        raise, so it isn't a hard failure, but the result is unconfirmed
        and worth surfacing distinctly rather than silently trusting it."""
        return [
            r.table_name for r in self.results
            if r.succeeded and not r.skipped and r.validation is not None and not r.validation.ok
        ]


def migrate_table(
    source,  # OracleConnector | MySQLConnector | ... any connector with fetch_batches()
    target,  # PostgresConnector | MySQLConnector | SqlServerConnector | Db2Connector | ...
    table: Table,
    batch_size: int = 5000,
    progress_cb: Optional[Callable[[str, int], None]] = None,
    retry_policy: Optional[RetryPolicy] = None,
    on_retry: Optional[Callable[[str, int, Exception, float], None]] = None,
    checkpoint=None,  # app_storage.TableCheckpoint | app_storage.ShardCheckpoint | None
    on_checkpoint_update: Optional[Callable[[], None]] = None,
    validate: bool = True,
    on_checkpoint_flush: Optional[Callable[[], None]] = None,
    checksum_max_rows: int = 50000,
    lob_batch_size: Optional[int] = 2000,
    shard=None,  # sharding.Shard | None
    pause_event: Optional["threading.Event"] = None,
) -> MigrationResult:
    """Migrate one table -- or, when `shard` is given, one disjoint slice
    of one table (see tgdatabridge.core.sharding).

    A shard narrows the `SELECT` with the shard's own `WHERE` predicate
    and nothing else changes: batching, retry, checkpointing and the
    running checksum all work per shard exactly as they work per table.
    `validate` should be False for an individual shard -- a shard covers
    only part of the table, so a row count against the whole target table
    would never match. migrate_schema turns validation off per shard and
    validates once after combining every shard's result (see
    _validate_combined_shards).

    `pause_event`, if given, is a `threading.Event` used as a run/pause
    flag: SET means "keep going" (the default state a fresh Event has
    none of, so callers must `.set()` it before starting), CLEARED means
    "pause here". Checked once per batch, between finishing the previous
    batch and starting the next -- `Event.wait()` blocks the calling
    thread for free while cleared and returns immediately once another
    thread sets it again, so pausing costs nothing but a blocked thread
    (the source/target connections stay open and idle, not torn down) and
    resuming needs no reconnect. Never checked mid-batch: a single
    `INSERT`/`COPY` already in flight always finishes, so pausing can
    never leave a batch half-written. None (the default) never pauses,
    exactly as before this parameter existed.
    """
    # Two checkpoint callbacks, deliberately distinct (SCALE.md section
    # 1.5). `on_checkpoint_update` means "progress advanced" and fires
    # after every batch -- the caller is free to coalesce those, and
    # app_storage.CheckpointWriter.request does. `on_checkpoint_flush`
    # means "this state must not be lost" and fires only on a terminal
    # transition (done/failed), which is exactly what a resume depends
    # on. It defaults to on_checkpoint_update so every caller written
    # before this distinction existed behaves precisely as it did.
    if on_checkpoint_flush is None:
        on_checkpoint_flush = on_checkpoint_update

    result = MigrationResult(table_name=table.name)
    if shard is not None:
        result.shard_key = shard.key
        result.shard_count = shard.total
    effective_batch_size = _effective_batch_size(table, batch_size, lob_batch_size)
    # Per an explicit request after a real migration reported false
    # "checksum mismatch, row count matched" results on LOB-heavy tables:
    # skip the checksum entirely (not just the comparison) for a table
    # with a CLOB/NCLOB/BLOB/LONG column. Two independent reasons this is
    # the right default, not just a workaround: (1) there is no portable,
    # driver-agnostic guarantee that a LOB value round-trips through
    # source and target byte-for-byte in its *serialization* even when
    # the content is identical (encoding, chunk-boundary, or whitespace
    # differences some drivers introduce), which is exactly the false
    # positive this was producing; (2) hashing a large LOB value is real
    # CPU work done twice -- once while streaming it out of the source,
    # again re-reading it from the target during validation -- and skipping
    # that is itself part of the speed-up asked for here. Row-count
    # validation (see below) still runs unchanged, so a LOB table that
    # loses rows is still caught; only the byte-for-byte content check is
    # skipped. `result.checksum = None` reuses the same "not comparable"
    # sentinel already used for a resumed shard with a stale checksum
    # (see MigrationResult's own docstring) -- _combine_shard_results
    # already treats None correctly when folding shards together.
    skip_checksum = _table_has_lob_columns(table)
    try:
        # MongoConnector (and only MongoConnector so far) defines
        # fetch_batches_table instead of/in addition to fetch_batches --
        # see this module's own docstring for why a SQL string can't
        # express a MongoDB-sourced migration for a synthesized child
        # table. Every other connector falls straight through to the
        # unchanged SQL-string path below. Wrapped in a zero-arg callable
        # (rather than called once, up front, as before) because a
        # source-connection reconnect (see below) needs to re-execute
        # this fetch from scratch on a fresh connection -- the original
        # generator is dead once its connection has been torn down.
        if hasattr(source, "fetch_batches_table"):
            # sharding.plan_shards never shards one of these sources (there
            # is no WHERE clause to attach a predicate to), so `shard` here
            # is always the degenerate whole-table shard.
            def start_batches():
                return source.fetch_batches_table(table, batch_size=effective_batch_size)
        else:
            # Every identifier is quoted: a real schema has columns
            # called `primary`, `key`, `order`, `group`, `index` and
            # `class`, and unquoted those are reserved words that make
            # the whole SELECT a syntax error -- MySQL 1064, and the
            # equivalent on every other engine. See tgdatabridge.db.source_sql.
            from tgdatabridge.db.source_sql import qualified_table, quote_columns
            select_cols = quote_columns(source, [c.name for c in table.columns])
            sql = (f"SELECT {select_cols} FROM "
                   f"{qualified_table(source, table.name, table.schema)}")
            if shard is not None and shard.where_sql:
                sql += f" WHERE {shard.where_sql}"
            # See _resume_order_columns' own docstring: without this, a
            # resumed migration's batch-skip logic below can silently
            # duplicate or drop rows, because nothing otherwise guarantees
            # this SELECT re-reads them back in the same order it did the
            # first time. table_columns guards against a primary key that
            # (unusually) isn't part of the columns being migrated -- an
            # ORDER BY naming a column absent from this SELECT is just a
            # different bug (a query error on every engine here).
            order_columns = _resume_order_columns(table)
            if order_columns:
                table_columns = {c.name for c in table.columns}
                if all(col in table_columns for col in order_columns):
                    order_cols_sql = quote_columns(source, order_columns)
                    sql += f" ORDER BY {order_cols_sql}"

            def start_batches():
                return source.fetch_batches(sql, batch_size=effective_batch_size)

        # The target table has to be able to accept these columns before a
        # single row is written. Without this the run gets as far as the
        # first INSERT and fails with a driver error ("Unknown column
        # 'employee_id' in 'field list'") naming neither the cause -- a
        # pre-existing table that CREATE TABLE IF NOT EXISTS declined to
        # touch -- nor what to do about it. See tgdatabridge.core.target_shape.
        shape_problem = check_table_shape(
            target, table.name, [c.name for c in table.columns],
            schema=getattr(target, "schema_name", None),
        )
        if shape_problem is not None:
            result.error = shape_problem.message
            result.succeeded = False
            if checkpoint is not None:
                checkpoint.status = "failed"
                if on_checkpoint_update:
                    on_checkpoint_update()
            return result

        def attempt():
            """One full read-the-source/write-the-target pass for this
            table (or shard). Re-reads `checkpoint` fresh on every call
            rather than closing over the outer already_written_batches/
            total -- that's what makes it safe to call more than once:
            a second call, after a source reconnect, picks up exactly
            where the checkpoint says the first call left off, instead
            of re-inserting rows the first call already wrote."""
            already_written_batches = checkpoint.batches_completed if checkpoint else 0
            total = checkpoint.rows_copied if checkpoint else 0
            checksum = None if skip_checksum else 0
            batch_index = 0
            last_columns: Optional[List[str]] = None

            for columns, rows in start_batches():
                last_columns = columns
                batch_index += 1

                if pause_event is not None:
                    pause_event.wait()

                if batch_index <= already_written_batches:
                    # Resuming: this batch's rows were already written to
                    # the target on a previous run (per the checkpoint), so
                    # writing them again would duplicate rows there. The
                    # source read itself can't be skipped -- there is no
                    # portable SQL OFFSET plumbed through fetch_batches/
                    # fetch_batches_table -- but the running checksum is
                    # still folded in here (unless skipped for a LOB table)
                    # so a resumed migration's final checksum matches what
                    # a single uninterrupted run would have produced.
                    if not skip_checksum:
                        for row in rows:
                            checksum ^= row_checksum(row)
                    continue

                if retry_policy is not None:
                    retry_call(
                        target.insert_batch, table.name, columns, rows,
                        policy=retry_policy,
                        on_retry=(
                            (lambda attempt_n, exc, delay: on_retry(table.name, attempt_n, exc, delay))
                            if on_retry else None
                        ),
                        # See retry.py's own docstring for why this exists: a
                        # connection genuinely torn down by the network or the
                        # database (the exact "DPY-4011: the database or
                        # network closed the connection" a real 8+ hour
                        # migration hit) fails identically on every retry
                        # without this -- re-sending insert_batch() on a dead
                        # connection object was never going to succeed.
                        # hasattr guards a target that duck-types
                        # insert_batch() without connect()/close() at all
                        # (this tool's own test doubles, and any future
                        # connector that manages its connection differently).
                        reconnect=_reconnect_callback(target),
                    )
                else:
                    target.insert_batch(table.name, columns, rows)

                total += len(rows)
                if not skip_checksum:
                    for row in rows:
                        checksum ^= row_checksum(row)

                if checkpoint is not None:
                    checkpoint.status = "in_progress"
                    checkpoint.rows_copied = total
                    checkpoint.batches_completed = batch_index
                    if on_checkpoint_update:
                        on_checkpoint_update()

                if progress_cb:
                    progress_cb(shard.label if shard is not None else table.name, total)

            return total, checksum, last_columns

        # A source connection that the network or the database itself
        # tore down mid-fetch -- Oracle's "DPY-4011: the database or
        # network closed the connection", hit on a real migration while
        # streaming a LOB-heavy table -- kills the `start_batches()`
        # generator outright; nothing about resuming *reading* recovers
        # by itself the way a target-side retry does. Retrying the whole
        # attempt (which re-executes the SELECT on a freshly reconnected
        # source) is only safe when a checkpoint exists to remember which
        # batches this same call already wrote -- otherwise a retry would
        # re-insert everything from the top and duplicate every row sent
        # before the drop. Without a checkpoint this table still fails
        # (as it always did) but the source is still reconnected in the
        # `except` block below before returning, so at least the *next*
        # table's migrate_table call doesn't inherit a dead connection --
        # see that block's own comment for why that mattered on a real
        # run (17 tables in a row failing with "DPY-1001: not connected
        # to database" once the first one lost its connection).
        if retry_policy is not None and checkpoint is not None:
            total, checksum, last_columns = retry_call(
                attempt, policy=retry_policy,
                on_retry=(
                    (lambda attempt_n, exc, delay: on_retry(table.name, attempt_n, exc, delay))
                    if on_retry else None
                ),
                reconnect=_reconnect_callback(source),
            )
        else:
            total, checksum, last_columns = attempt()

        result.rows_copied = total
        result.succeeded = True
        result.checksum = checksum
        result.columns = last_columns

        if checkpoint is not None:
            checkpoint.status = "done"
            checkpoint.rows_copied = total
            # Only a ShardCheckpoint carries a checksum -- it's what lets a
            # resumed run recombine an already-finished shard's
            # contribution without re-reading it. setattr-style guarding
            # keeps a plain TableCheckpoint (no such field) working.
            if hasattr(checkpoint, "checksum"):
                checkpoint.checksum = checksum
            if on_checkpoint_flush:
                on_checkpoint_flush()

        if validate and last_columns is not None:
            schema_name = getattr(target, "schema_name", None)
            result.validation = validate_table(
                target, table.name, last_columns, total,
                expected_checksum=checksum, schema=schema_name,
                checksum_max_rows=checksum_max_rows,
            )
    except Exception as exc:  # noqa: BLE001 - report to the UI, don't crash the batch
        result.error = str(exc)
        result.succeeded = False
        # Best-effort, regardless of whether a checkpoint let this table's
        # own attempt recover above: a source connection left dead here
        # would otherwise be handed straight to the *next* table's
        # migrate_table call, which fails instantly with "not connected to
        # database" -- exactly what turned one dropped connection into 17
        # more failed tables on a real run. Reconnecting costs nothing when
        # the failure wasn't connection-related (is_transient(exc) is
        # False, so this is skipped), and a reconnect that itself fails is
        # swallowed -- there is nothing more useful to do with that error
        # than let the next table's own attempt discover the source is
        # still unreachable.
        if is_transient(exc):
            reconnect_source = _reconnect_callback(source)
            if reconnect_source is not None:
                try:
                    reconnect_source()
                except Exception:  # noqa: BLE001
                    pass
        if checkpoint is not None:
            checkpoint.status = "failed"
            if on_checkpoint_flush:
                on_checkpoint_flush()
    return result


def order_tables_by_dependency_waves(tables: List[Table]) -> List[List[Table]]:
    """Groups tables into ordered "waves": every table in wave N has all of
    its FK dependencies satisfied by tables in waves 0..N-1 (never by
    another table in wave N itself), so every table within one wave can
    safely be migrated in any order -- including concurrently -- as long
    as no later wave starts until the current one has fully finished. This
    is the same Kahn's-algorithm topological sort order_tables_by_dependency
    has always used, just exposing each iteration's "ready" set as its own
    wave instead of flattening straight into one list (see
    order_tables_by_dependency's own docstring for the underlying
    FK-ordering problem and the cycle fallback, both unchanged here --
    flattening this function's output reproduces that function's exact
    output order, byte for byte, which is what lets it now be defined in
    terms of this one instead of duplicating the algorithm)."""
    by_key = {t.name.upper(): t for t in tables}
    deps = {t.name.upper(): set() for t in tables}
    for t in tables:
        key = t.name.upper()
        for cons in t.constraints:
            if cons.kind == "FOREIGN KEY" and cons.ref_table:
                ref_key = cons.ref_table.upper()
                if ref_key in by_key and ref_key != key:
                    deps[key].add(ref_key)

    waves: List[List[Table]] = []
    placed: set = set()
    remaining = list(tables)
    while remaining:
        ready = [t for t in remaining if deps[t.name.upper()] <= placed]
        if not ready:
            # Cycle: nothing left is fully satisfied. Append what's left as
            # one final wave, in original order, rather than looping
            # forever -- see order_tables_by_dependency's own docstring.
            waves.append(remaining)
            break
        ready_ids = {id(t) for t in ready}
        for t in ready:
            placed.add(t.name.upper())
        waves.append(ready)
        remaining = [t for t in remaining if id(t) not in ready_ids]
    return waves


def order_tables_by_dependency(tables: List[Table]) -> List[Table]:
    """Order tables so a table referenced by another table's foreign key is
    migrated first.

    This is the data-migration counterpart of the FK-ordering problem
    already solved for DDL generation -- except DDL could sidestep it by
    deferring all ALTER TABLE ADD CONSTRAINT statements to a second pass
    after every table exists, whereas by the time data migration runs the
    target's FK constraints are already active (DDL was already applied),
    so row insertion order must itself respect dependencies. Migrating a
    child table before its parent's rows are loaded fails immediately with
    a foreign key violation on the child's very first batch.

    A thin flattening wrapper over order_tables_by_dependency_waves -- see
    that function's own docstring for the algorithm (Kahn's algorithm) and
    the cycle-fallback behavior."""
    return [t for wave in order_tables_by_dependency_waves(tables) for t in wave]


def migrate_schema(
    source,  # OracleConnector | MySQLConnector | ... any connector with fetch_batches() -- ignored when max_workers > 1, see below
    target,
    tables: List[Table],
    batch_size: int = 5000,
    progress_cb: Optional[Callable[[str, int], None]] = None,
    table_progress_cb: Optional[Callable[[int, int], None]] = None,
    retry_policy: Optional[RetryPolicy] = None,
    on_retry: Optional[Callable[[str, int, Exception, float], None]] = None,
    checkpoint=None,  # app_storage.MigrationCheckpoint | None
    on_checkpoint_update: Optional[Callable[[], None]] = None,
    validate: bool = True,
    on_checkpoint_flush: Optional[Callable[[], None]] = None,
    checksum_max_rows: int = 50000,
    lob_batch_size: Optional[int] = 2000,
    max_workers: int = 1,
    source_factory: Optional[Callable[[], object]] = None,
    target_factory: Optional[Callable[[], object]] = None,
    max_shards_per_table: int = 1,
    min_rows_to_shard: int = sharding.DEFAULT_MIN_ROWS_TO_SHARD,
    lob_min_rows_to_shard: Optional[int] = 20_000,
    on_shard_plan: Optional[Callable[[str, int, str], None]] = None,
    pause_event: Optional["threading.Event"] = None,
) -> MigrationReport:
    """`progress_cb(table_name, rows_copied_so_far)` reports row-level
    progress within whichever table is currently migrating (existing
    behavior, used for the log console). `table_progress_cb(tables_done,
    total_tables)` additionally reports table-level progress once each
    table finishes -- on a schema with thousands of tables, that's what
    lets the GUI show a real percentage instead of an indeterminate spinner
    for the whole run.

    `checkpoint`, if given, makes this call resumable: a table already
    marked "done" on it is skipped entirely (reported back as a
    `MigrationResult(skipped=True)` carrying its previously-recorded row
    count, not re-read from the source at all), and a table marked
    "in_progress" resumes via migrate_table's own batch-skipping logic --
    see that function's docstring. `checkpoint.tables` is populated with a
    fresh `TableCheckpoint()` for any table not already on it, so the very
    first call with a given checkpoint object behaves identically to not
    passing one at all, just with progress now being recorded as it goes.

    `max_workers` (default 1) controls parallel table migration -- see
    this module's own docstring for the "Scale features" overview.
    max_workers=1 uses the `source`/`target` connectors passed in directly,
    single-threaded, exactly as before this feature existed. max_workers>1
    instead REQUIRES `source_factory`/`target_factory` (each a zero-arg
    callable returning one fresh, already-connected connector instance --
    e.g. `lambda: OracleConnector(params)` wrapped in something that also
    calls `.connect()`) and ignores `source`/`target` entirely: a bare
    DB-API connection object is not documented as safe to share across
    threads by any driver this tool uses, so each worker thread needs its
    own connection, drawn from a small tgdatabridge.db.pool.ConnectionPool built
    from the factories and sized to `max_workers`, not the single
    connection `source`/`target` represent. Passing max_workers>1 without
    both factories raises ValueError immediately, before anything is
    migrated, rather than silently falling back to single-threaded mode.

    `lob_min_rows_to_shard` (default 20,000) is a much lower version of
    `min_rows_to_shard`, applied only to a table with a CLOB/NCLOB/BLOB/
    LONG/LONG RAW column (see _table_has_lob_columns). A table's cost to
    migrate is driven by *data volume*, not row count, and nowhere is that
    gap bigger than a LOB table: a real migration had a 92,500-row LOB
    table -- 90%+ below the plain 1,000,000-row sharding threshold, so it
    was never split -- take 25 minutes on one connection while tables with
    10-40x more (non-LOB) rows finished in well under a minute, because
    the single sequential fetch/COPY loop had no way to use the other
    workers sitting idle once the small tables were done. Splitting a LOB
    table into shards the same way a huge plain table already is (still
    gated on every one of plan_shards' own safety checks -- a
    single-column integer primary key, and `max_shards_per_table` (i.e.
    `max_workers`) actually being > 1) lets several connections pull and
    write its rows at once instead of one connection carrying the whole
    table's document bytes alone. Pass `lob_min_rows_to_shard=None` to
    disable this and use the plain `min_rows_to_shard` for every table,
    LOB or not, restoring the pre-this-feature behavior exactly."""
    from tgdatabridge.utils.app_storage import ShardCheckpoint, TableCheckpoint  # local import: keeps migrator.py free of a hard app_storage dependency for callers that never use checkpoints

    # See migrate_table for what distinguishes these two. Defaulting flush
    # to update keeps every pre-existing caller behaving exactly as before.
    if on_checkpoint_flush is None:
        on_checkpoint_flush = on_checkpoint_update

    report = MigrationReport()
    waves = order_tables_by_dependency_waves(tables)
    total = sum(len(w) for w in waves)

    if max_workers <= 1:
        done = 0
        for wave in waves:
            for table in wave:
                done += 1
                table_checkpoint = None
                if checkpoint is not None:
                    table_checkpoint = checkpoint.tables.setdefault(table.name, TableCheckpoint())
                    if table_checkpoint.status == "done":
                        report.results.append(MigrationResult(
                            table_name=table.name, rows_copied=table_checkpoint.rows_copied,
                            succeeded=True, skipped=True,
                        ))
                        if table_progress_cb:
                            table_progress_cb(done, total)
                        continue

                result = migrate_table(
                    source, target, table, batch_size, progress_cb,
                    retry_policy=retry_policy, on_retry=on_retry,
                    checkpoint=table_checkpoint, on_checkpoint_update=on_checkpoint_update,
                    validate=validate, checksum_max_rows=checksum_max_rows,
                    lob_batch_size=lob_batch_size, on_checkpoint_flush=on_checkpoint_flush,
                    pause_event=pause_event,
                )
                report.results.append(result)
                if table_progress_cb:
                    table_progress_cb(done, total)
        return report

    if source_factory is None or target_factory is None:
        raise ValueError(
            "migrate_schema(max_workers>1) requires both source_factory and target_factory "
            "(zero-arg callables each returning one fresh, already-connected connector) -- "
            "sharing a single connection across worker threads isn't safe for any connector "
            "this tool uses. See migrate_schema's own docstring."
        )

    source_pool = ConnectionPool(source_factory, max_size=max_workers)
    target_pool = ConnectionPool(target_factory, max_size=max_workers)
    # Serializes anything below that mutates shared state from more than
    # one worker thread at once: the checkpoint-update callback (typically
    # app_storage.save_checkpoint, writing the *whole* checkpoint file --
    # not safe to call concurrently even though each table has its own
    # TableCheckpoint object), and progress/retry callbacks (usually just a
    # log line each -- safe either way, but serialized too so log output
    # from different tables' workers doesn't interleave mid-line).
    callback_lock = threading.Lock()
    done_counter = {"n": 0}

    def locked_progress(table_name, count):
        if progress_cb:
            with callback_lock:
                progress_cb(table_name, count)

    def locked_on_retry(table_name, attempt, exc, delay):
        if on_retry:
            with callback_lock:
                on_retry(table_name, attempt, exc, delay)

    def locked_on_checkpoint_update():
        if on_checkpoint_update:
            with callback_lock:
                on_checkpoint_update()

    def locked_on_checkpoint_flush():
        if on_checkpoint_flush:
            with callback_lock:
                on_checkpoint_flush()

    def run_one(table: Table, shard) -> MigrationResult:
        """Migrate one unit of work -- one whole table, or one shard of
        one table. The unit is a shard rather than a table specifically so
        a single huge table can occupy several workers at once instead of
        pinning one worker while the rest go idle (SCALE.md section 1.2)."""
        table_checkpoint = None
        unit_checkpoint = None
        if checkpoint is not None:
            with callback_lock:
                table_checkpoint = checkpoint.tables.setdefault(table.name, TableCheckpoint())
                if shard.is_whole_table:
                    unit_checkpoint = table_checkpoint
                else:
                    unit_checkpoint = table_checkpoint.shards.setdefault(shard.key, ShardCheckpoint())
            if unit_checkpoint.status == "done":
                result = MigrationResult(
                    table_name=table.name, rows_copied=unit_checkpoint.rows_copied,
                    succeeded=True, skipped=True,
                    shard_key=None if shard.is_whole_table else shard.key,
                    shard_count=shard.total,
                    # A finished shard still has to contribute its checksum,
                    # or the combined table-level validation after a resume
                    # would compare a partial checksum against the target's
                    # complete one and report a spurious mismatch.
                    checksum=(
                        None if getattr(checkpoint, "checksums_stale", False)
                        else getattr(unit_checkpoint, "checksum", 0)
                    ),
                )
                with callback_lock:
                    done_counter["n"] += 1
                    if table_progress_cb:
                        table_progress_cb(done_counter["n"], total)
                return result

        result = _migrate_one_pooled(
            source_pool, target_pool, table, batch_size,
            locked_progress if progress_cb else None,
            retry_policy, locked_on_retry if on_retry else None,
            unit_checkpoint, locked_on_checkpoint_update if on_checkpoint_update else None,
            # A shard covers only part of the table, so validating it
            # against the whole target table would never match. Sharded
            # tables are validated once, after every shard finishes --
            # see _validate_combined_shards.
            validate and shard.is_whole_table,
            checksum_max_rows, lob_batch_size, shard,
            locked_on_checkpoint_flush if on_checkpoint_flush else None,
            pause_event,
        )
        with callback_lock:
            done_counter["n"] += 1
            if table_progress_cb:
                table_progress_cb(done_counter["n"], total)
        return result

    try:
        for wave in waves:
            # Plan shards for this wave up front, using a pooled source
            # connection: planning reads MIN/MAX per shardable table, which
            # needs a real connection, and doing it per wave rather than
            # once at the start means a table's row count is read as late
            # as possible.
            wave_units = _plan_wave_units(
                source_pool, wave, max_shards_per_table, min_rows_to_shard,
                lob_min_rows_to_shard, on_shard_plan, callback_lock,
            )
            # `total` was computed as a table count before sharding existed;
            # with shards the meaningful denominator for the progress bar is
            # the number of work units actually being run.
            total = len(wave_units)
            done_counter["n"] = 0
            workers_for_wave = min(max_workers, len(wave_units))
            with ThreadPoolExecutor(max_workers=workers_for_wave) as executor:
                futures = [executor.submit(run_one, table, shard) for table, shard in wave_units]
                # Collecting in submission order (== wave order == original
                # input order within the wave) keeps MigrationReport.results
                # deterministic regardless of which worker happened to
                # finish first -- the same guarantee the single-threaded
                # path above already gives for free.
                shard_results = [future.result() for future in futures]

            report.results.extend(_combine_shard_results(
                target_pool, wave, shard_results, validate, checksum_max_rows, checkpoint,
                # A table reaching "done" is exactly the record a resume
                # reads to skip it entirely -- never coalesced away.
                locked_on_checkpoint_flush if on_checkpoint_flush else None,
            ))
    finally:
        source_pool.close_all()
        target_pool.close_all()

    return report


def _plan_wave_units(source_pool, wave, max_shards_per_table, min_rows_to_shard,
                      lob_min_rows_to_shard, on_shard_plan, callback_lock):
    """Expand one dependency wave's tables into `(table, shard)` work
    units. A table that can't or shouldn't be sharded contributes exactly
    one whole-table unit, which is byte-for-byte the pre-sharding
    behavior.

    `lob_min_rows_to_shard`, when not None, replaces `min_rows_to_shard`
    for any table _table_has_lob_columns flags -- see migrate_schema's own
    docstring for why a LOB table needs a much lower bar to be worth
    splitting than a plain one does."""
    units = []
    if max_shards_per_table <= 1:
        for table in wave:
            units.extend((table, shard) for shard in sharding.plan_shards(
                None, table, max_shards=1, strategy=sharding.STRATEGY_NONE))
        return units

    with source_pool.connection() as source:
        for table in wave:
            effective_min_rows = min_rows_to_shard
            if lob_min_rows_to_shard is not None and _table_has_lob_columns(table):
                effective_min_rows = lob_min_rows_to_shard
            try:
                shards = sharding.plan_shards(
                    source, table, max_shards=max_shards_per_table, min_rows=effective_min_rows)
            except Exception:  # noqa: BLE001 - planning must never fail a migration; fall back to one unit
                shards = sharding.plan_shards(
                    None, table, max_shards=1, strategy=sharding.STRATEGY_NONE)
            if on_shard_plan:
                with callback_lock:
                    on_shard_plan(table.name, len(shards), shards[0].reason)
            units.extend((table, shard) for shard in shards)
    return units


def _combine_shard_results(
    target_pool, wave, shard_results, validate, checksum_max_rows, checkpoint, on_checkpoint_flush,
):
    """Fold every shard's result back into one MigrationResult per table,
    then validate each sharded table once against the target.

    Combining is straightforward because of a property the checksum
    already had: `validation.table_checksum` XOR-combines per-row hashes,
    and XOR is commutative and associative, so the checksum of the whole
    table is exactly the XOR of its shards' checksums -- no matter what
    order the shards ran in or finished in. Row counts simply add, since
    the shards are disjoint by construction (see sharding.build_shards).
    """
    from tgdatabridge.utils.app_storage import TableCheckpoint  # local import, same reason as migrate_schema's

    by_table = {}
    order = []
    for result in shard_results:
        if result.table_name not in by_table:
            by_table[result.table_name] = []
            order.append(result.table_name)
        by_table[result.table_name].append(result)

    tables_by_name = {t.name: t for t in wave}
    combined = []
    for table_name in order:
        results = by_table[table_name]
        if len(results) == 1 and results[0].shard_key is None:
            combined.append(results[0])  # unsharded -- already complete, validation included
            continue

        merged = MigrationResult(
            table_name=table_name,
            rows_copied=sum(r.rows_copied for r in results),
            succeeded=all(r.succeeded for r in results),
            skipped=all(r.skipped for r in results),
            shard_count=results[0].shard_count,
        )
        errors = [f"{r.shard_key}: {r.error}" for r in results if r.error]
        if errors:
            merged.error = "; ".join(errors)

        # A single non-comparable shard makes the combined value
        # meaningless -- XORing it against the target's complete checksum
        # would report a mismatch that says nothing about the data.
        if any(r.checksum is None for r in results):
            checksum = None
        else:
            checksum = 0
            for r in results:
                checksum ^= r.checksum
        merged.checksum = checksum
        columns = next((r.columns for r in results if r.columns), None)
        merged.columns = columns

        # Only validate a table whose shards all actually succeeded --
        # validating a partially-failed table would report a row-count
        # mismatch that just restates the failure already recorded above.
        if validate and merged.succeeded and columns:
            merged.validation = _validate_combined_shards(
                target_pool, tables_by_name.get(table_name), table_name, columns,
                merged.rows_copied, checksum, checksum_max_rows,
            )

        if checkpoint is not None and merged.succeeded:
            table_checkpoint = checkpoint.tables.setdefault(table_name, TableCheckpoint())
            table_checkpoint.status = "done"
            table_checkpoint.rows_copied = merged.rows_copied
            if on_checkpoint_flush:
                on_checkpoint_flush()

        combined.append(merged)
    return combined


def _validate_combined_shards(
    target_pool, table, table_name, columns, expected_rows, expected_checksum, checksum_max_rows,
):
    try:
        with target_pool.connection() as target:
            schema_name = getattr(target, "schema_name", None)
            return validate_table(
                target, table_name, columns, expected_rows,
                expected_checksum=expected_checksum, schema=schema_name,
                checksum_max_rows=checksum_max_rows,
            )
    except Exception as exc:  # noqa: BLE001 - an unvalidatable table is "unverified", not a failed migration
        return ValidationResult(
            table_name=table_name, expected_rows=expected_rows, error=str(exc))


def _migrate_one_pooled(
    source_pool: ConnectionPool,
    target_pool: ConnectionPool,
    table: Table,
    batch_size: int,
    progress_cb,
    retry_policy,
    on_retry,
    table_checkpoint,
    on_checkpoint_update,
    validate: bool,
    checksum_max_rows: int,
    lob_batch_size: Optional[int],
    shard=None,
    on_checkpoint_flush=None,
    pause_event: Optional["threading.Event"] = None,
) -> MigrationResult:
    """Acquires one connection from each pool for the duration of one
    work unit's migration, then returns them -- used by migrate_schema's
    max_workers>1 path, one call per worker-thread unit (a whole table, or
    one shard of one table). Connection acquisition/release failures (e.g.
    the factory itself raises) are caught here too, not just inside
    migrate_table, so a single bad connection can't crash the whole worker
    pool -- it comes back as an ordinary failed MigrationResult instead,
    exactly like any other per-unit migration error."""
    try:
        with source_pool.connection() as source, target_pool.connection() as target:
            return migrate_table(
                source, target, table, batch_size, progress_cb,
                retry_policy=retry_policy, on_retry=on_retry,
                checkpoint=table_checkpoint, on_checkpoint_update=on_checkpoint_update,
                validate=validate, checksum_max_rows=checksum_max_rows,
                lob_batch_size=lob_batch_size, shard=shard,
                on_checkpoint_flush=on_checkpoint_flush,
                pause_event=pause_event,
            )
    except Exception as exc:  # noqa: BLE001 - a pool/connection-level failure must not crash the executor; report it like any other per-unit failure
        result = MigrationResult(
            table_name=table.name, succeeded=False, error=str(exc),
            shard_key=None if (shard is None or shard.is_whole_table) else shard.key,
            shard_count=shard.total if shard is not None else 1,
        )
        if table_checkpoint is not None:
            table_checkpoint.status = "failed"
            terminal_cb = on_checkpoint_flush or on_checkpoint_update
            if terminal_cb:
                terminal_cb()
        return result


# --------------------------------------------------------- dry-run / plan mode


@dataclass
class TablePlan:
    table_name: str
    source_rows: Optional[int] = None
    target_exists: Optional[bool] = None
    target_rows_before: Optional[int] = None
    ready: bool = False
    error: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


@dataclass
class MigrationPlan:
    tables: List[TablePlan] = field(default_factory=list)

    @property
    def total_source_rows(self) -> int:
        return sum(t.source_rows or 0 for t in self.tables)

    @property
    def not_ready(self) -> List[str]:
        return [t.table_name for t in self.tables if not t.ready]


def plan_table(source, target, table: Table) -> TablePlan:
    """A read-only feasibility check for one table's migration: how many
    rows the source has (if the source connector can report that cheaply)
    and whether the target table exists and is reachable (and, if so, how
    many rows it already has). Writes nothing to either side -- safe to
    run repeatedly or speculatively, e.g. against a live production
    source, unlike an actual migrate_table() call.

    A MongoDB-sourced synthesized child table (`table.source_array_path`
    is not None -- see mongo_source_introspector._build_child_table) has
    no real collection of its own to count directly; its row count is only
    known once migrate_table actually runs and unwinds the parent array,
    so this reports that explicitly via a warning rather than guessing."""
    plan = TablePlan(table_name=table.name)
    try:
        if getattr(table, "source_array_path", None) is not None:
            plan.warnings.append(
                f"{table.name} is a synthesized child table (array-unwind source) -- its row count "
                "is only known once the migration actually runs."
            )
        else:
            source_count = getattr(source, "count_rows", None)
            if source_count is not None:
                source_table_name = getattr(table, "source_collection", None) or table.name
                plan.source_rows = source_count(source_table_name, schema=table.schema)
            else:
                plan.warnings.append(
                    f"{type(source).__name__} has no count_rows() -- source row count unknown "
                    "until the migration actually runs."
                )

        target_count = getattr(target, "count_rows", None)
        if target_count is None:
            plan.warnings.append(
                f"{type(target).__name__} has no count_rows() -- target readiness unknown until "
                "the migration actually runs."
            )
        else:
            try:
                plan.target_rows_before = target_count(table.name, schema=getattr(target, "schema_name", None))
                plan.target_exists = True
                if plan.target_rows_before:
                    plan.warnings.append(
                        f"Target table already has {plan.target_rows_before} row(s) -- migrating now "
                        "would add to them, not replace them."
                    )
            except Exception:  # noqa: BLE001 - most likely "table doesn't exist yet", not fatal to the plan
                plan.target_exists = False
                plan.warnings.append(
                    "Target table doesn't appear to exist yet, or isn't reachable -- make sure DDL "
                    "has been applied to the target (step 3) before migrating data."
                )

        if plan.target_exists:
            # Dry Run is meant to answer "would this work?" -- an existing
            # table with the wrong columns is precisely the case where the
            # answer is no and nothing else here would notice.
            problem = check_table_shape(
                target, table.name, [c.name for c in table.columns],
                schema=getattr(target, "schema_name", None),
            )
            if problem is not None:
                plan.warnings.append(problem.message)
                plan.target_exists = False

        plan.ready = plan.target_exists is not False
    except Exception as exc:  # noqa: BLE001 - a failed plan check is reported, not fatal
        plan.error = str(exc)
        plan.ready = False
    return plan


def plan_schema(
    source, target, tables: List[Table], progress_cb: Optional[Callable[[int, int], None]] = None,
) -> MigrationPlan:
    """The dry-run counterpart of migrate_schema(): the same table
    ordering, but plan_table() in place of migrate_table() -- nothing is
    written to either the source or the target. Meant to be run before a
    real migrate_schema() call, to catch an unreachable/missing target
    table or get a row-count estimate without risking any data."""
    plan = MigrationPlan()
    ordered = order_tables_by_dependency(tables)
    total = len(ordered)
    for done, table in enumerate(ordered, start=1):
        plan.tables.append(plan_table(source, target, table))
        if progress_cb:
            progress_cb(done, total)
    return plan
