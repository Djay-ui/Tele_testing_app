"""Integration tests for sharded migration -- tgdatabridge.core.migrator's
shard-aware migrate_table plus migrate_schema's shard-level work queue
(SCALE.md section 1.2).

tests/test_sharding.py checks the shard *planner* in isolation. This file
checks the thing that actually matters end to end: that running a table's
shards through the migrator moves every row exactly once, that the
combined checksum validates against the target, and that a failed shard
resumes without re-copying the ones that succeeded.

The fake source below genuinely evaluates each shard's WHERE predicate
against its rows, so a planner bug that produced overlapping or gapped
ranges would show up here as duplicated or missing rows -- not just as a
different SQL string.
"""
import pathlib
import re
import tempfile

from tgdatabridge.core import migrator
from tgdatabridge.core.schema_model import Column, Constraint, Table
from tgdatabridge.core.validation import table_checksum
from tgdatabridge.utils import app_storage


def _table(name="BIG", pk_type="NUMBER(10)", with_pk=True, lob=False):
    table = Table(name=name, schema="HR")
    table.columns = [Column(name="ID", data_type=pk_type), Column(name="NAME", data_type="VARCHAR2(50)")]
    if lob:
        table.columns.append(Column(name="DOC", data_type="BLOB"))
    if with_pk:
        table.constraints = [Constraint(name=f"{name}_PK", kind="PRIMARY KEY", columns=["ID"])]
    return table


def _rows(n):
    return [(i, f"name-{i}") for i in range(1, n + 1)]


def _eval_predicate(where, value):
    # The predicate now arrives with the key column quoted -- "ID" on this
    # fake, which is quoted the way every non-MySQL/SQL-Server engine
    # does. See tgdatabridge.db.source_sql for why every identifier the migrator
    # emits is quoted.
    return eval(where.replace('"ID"', str(value)).replace("ID", str(value))
                     .replace(" AND ", " and "))


class _FakeSource:
    """Answers the MIN/MAX probe and really filters rows by the shard
    predicate it's handed."""

    def __init__(self, rows, low=None, high=None):
        self.rows = rows
        self.low = low if low is not None else min(r[0] for r in rows)
        self.high = high if high is not None else max(r[0] for r in rows)
        self.queries = []

    def connect(self):
        pass

    def close(self):
        pass

    def count_rows(self, table, schema=None):
        return len(self.rows)

    def execute(self, sql):
        return [(self.low, self.high)]

    def fetch_batches(self, sql, batch_size=5000):
        self.queries.append(sql)
        # migrate_table now appends an ORDER BY after the WHERE predicate
        # (see tgdatabridge.core.migrator._resume_order_columns' own
        # docstring for why: a resumed migration has to re-read rows back
        # in a stable order for its batch-skip logic to be correct).
        # Strip it before parsing the predicate below, or it would be
        # `eval`'d as part of it.
        where_clause = sql.split(" ORDER BY ")[0]
        match = re.search(r"WHERE (.+)$", where_clause)
        selected = [r for r in self.rows if (_eval_predicate(match.group(1), r[0]) if match else True)]
        for start in range(0, len(selected), batch_size):
            yield ["ID", "NAME"], selected[start:start + batch_size]


class _FakeTarget:
    def __init__(self, fail_on_first_id=None):
        self.rows = []
        self.fail_on_first_id = fail_on_first_id

    def connect(self):
        pass

    def close(self):
        pass

    @property
    def schema_name(self):
        return "public"

    def insert_batch(self, table, columns, rows):
        if self.fail_on_first_id is not None and rows and rows[0][0] == self.fail_on_first_id:
            raise RuntimeError(f"target rejected the batch starting at id={rows[0][0]}")
        self.rows.extend(rows)

    def count_rows(self, table, schema=None):
        return len(self.rows)

    def checksum_rows(self, table, columns, schema=None, sample_size=None):
        return table_checksum(self.rows)


def _run(table, source, target, shards=4, workers=4, min_rows=10, checkpoint=None,
         on_checkpoint_update=None, lob_min_rows=20_000):
    return migrator.migrate_schema(
        None, None, [table], batch_size=100,
        max_workers=workers, source_factory=lambda: source, target_factory=lambda: target,
        max_shards_per_table=shards, min_rows_to_shard=min_rows, lob_min_rows_to_shard=lob_min_rows,
        checkpoint=checkpoint, on_checkpoint_update=on_checkpoint_update,
    )


# ---------------------------------------------------- the correctness core

def test_sharded_migration_moves_every_row_exactly_once():
    rows = _rows(1000)
    source, target = _FakeSource(rows), _FakeTarget()
    _run(_table(), source, target)
    assert sorted(target.rows) == rows


def test_sharded_migration_issues_one_query_per_shard():
    source, target = _FakeSource(_rows(1000)), _FakeTarget()
    _run(_table(), source, target, shards=4)
    assert len(source.queries) == 4
    assert all("WHERE" in q for q in source.queries)


def test_sharded_result_is_reported_as_one_table():
    source, target = _FakeSource(_rows(1000)), _FakeTarget()
    report = _run(_table(), source, target, shards=4)
    assert len(report.results) == 1
    result = report.results[0]
    assert result.table_name == "BIG"
    assert result.rows_copied == 1000
    assert result.shard_count == 4
    assert result.succeeded is True


def test_combined_checksum_validates_against_the_target():
    # XOR is commutative and associative, so the whole table's checksum is
    # exactly the XOR of its shards' -- whatever order they finished in.
    source, target = _FakeSource(_rows(1000)), _FakeTarget()
    report = _run(_table(), source, target, shards=4)
    validation = report.results[0].validation
    assert validation is not None
    assert validation.actual_rows == 1000
    assert validation.checksums_match is True
    assert validation.ok is True


def test_rows_outside_the_planned_bounds_are_still_migrated():
    # MIN/MAX are read at planning time; the open-ended first and last
    # shards are what stop later inserts outside that window being lost.
    rows = _rows(100) + [(0, "zero"), (-7, "negative"), (5000, "far above")]
    source = _FakeSource(rows, low=1, high=100)
    target = _FakeTarget()
    _run(_table(), source, target)
    assert sorted(target.rows) == sorted(rows)


def test_more_shards_than_workers_still_partitions_correctly():
    rows = _rows(500)
    source, target = _FakeSource(rows), _FakeTarget()
    _run(_table(), source, target, shards=8, workers=2)
    assert sorted(target.rows) == rows


# ------------------------------------------------------------- fallbacks

def test_table_without_a_shardable_key_still_migrates_unsharded():
    rows = _rows(500)
    source, target = _FakeSource(rows), _FakeTarget()
    report = _run(_table(with_pk=False), source, target, shards=4)
    assert sorted(target.rows) == rows
    assert report.results[0].shard_count == 1
    assert source.queries and "WHERE" not in source.queries[0]


def test_max_shards_one_produces_the_unsharded_sql():
    source, target = _FakeSource(_rows(100)), _FakeTarget()
    _run(_table(), source, target, shards=1, workers=2)
    assert len(source.queries) == 1
    assert "WHERE" not in source.queries[0]


def test_small_table_is_not_sharded():
    source, target = _FakeSource(_rows(50)), _FakeTarget()
    _run(_table(), source, target, shards=4, min_rows=1000)
    assert len(source.queries) == 1


# --------------------------------------------- LOB-aware sharding threshold

def test_a_lob_table_below_the_plain_threshold_is_still_sharded():
    """The real case this exists for: a LOB table nowhere near
    min_rows_to_shard's usual bar, but still slow enough (per row, not per
    table) to be worth splitting across the idle workers -- see
    migrate_schema's own docstring."""
    source, target = _FakeSource(_rows(500)), _FakeTarget()
    _run(_table(lob=True), source, target, shards=4, min_rows=100_000, lob_min_rows=100)
    assert len(source.queries) == 4


def test_a_lob_table_below_both_thresholds_is_still_not_sharded():
    source, target = _FakeSource(_rows(50)), _FakeTarget()
    _run(_table(lob=True), source, target, shards=4, min_rows=100_000, lob_min_rows=1000)
    assert len(source.queries) == 1


def test_lob_min_rows_to_shard_none_disables_the_lower_threshold():
    """`lob_min_rows_to_shard=None` is the escape hatch back to exactly
    the pre-this-feature behavior: a LOB table is judged by the plain
    min_rows_to_shard like everything else, however low the LOB threshold
    would otherwise have been."""
    source, target = _FakeSource(_rows(500)), _FakeTarget()
    _run(_table(lob=True), source, target, shards=4, min_rows=100_000, lob_min_rows=None)
    assert len(source.queries) == 1


def test_a_plain_table_is_unaffected_by_the_lob_threshold():
    """A table with no LOB column must never be sharded early just because
    a low lob_min_rows_to_shard is configured -- only _table_has_lob_columns
    tables get the lower bar."""
    source, target = _FakeSource(_rows(500)), _FakeTarget()
    _run(_table(lob=False), source, target, shards=4, min_rows=100_000, lob_min_rows=100)
    assert len(source.queries) == 1


def test_shard_plan_callback_reports_the_split():
    seen = []
    migrator.migrate_schema(
        None, None, [_table()], batch_size=100,
        max_workers=4, source_factory=lambda: _FakeSource(_rows(1000)), target_factory=_FakeTarget,
        max_shards_per_table=4, min_rows_to_shard=10,
        on_shard_plan=lambda name, count, reason: seen.append((name, count, reason)),
    )
    assert len(seen) == 1
    name, count, reason = seen[0]
    assert name == "BIG"
    assert count == 4
    assert "pk_range" in reason


# --------------------------------------------------------------- failures

def test_one_failed_shard_fails_the_table_but_keeps_the_others_rows():
    source = _FakeSource(_rows(400))
    target = _FakeTarget(fail_on_first_id=201)  # third of four shards
    report = _run(_table(), source, target, shards=4)
    result = report.results[0]
    assert result.succeeded is False
    assert "3of4" in result.error
    # The shards that did work aren't rolled back -- that's what makes the
    # resume below cheap.
    assert result.rows_copied == 300


def test_a_partially_failed_table_is_not_validated():
    # Validating it would report a row-count mismatch that merely restates
    # the failure already recorded.
    source = _FakeSource(_rows(400))
    report = _run(_table(), source, _FakeTarget(fail_on_first_id=201), shards=4)
    assert report.results[0].validation is None


def test_failed_table_appears_in_failed_tables_once():
    source = _FakeSource(_rows(400))
    report = _run(_table(), source, _FakeTarget(fail_on_first_id=201), shards=4)
    assert report.failed_tables == ["BIG"]


# ---------------------------------------------------------------- resume

def _checkpoint():
    return app_storage.MigrationCheckpoint(
        checkpoint_id="cp-test", schema_name="HR",
        source_engine="Oracle", target_engine="PostgreSQL",
    )


def test_resume_recopies_only_the_failed_shard():
    rows = _rows(400)
    checkpoint = _checkpoint()

    first_target = _FakeTarget(fail_on_first_id=201)
    _run(_table(), _FakeSource(rows), first_target, shards=4, checkpoint=checkpoint)
    assert len(first_target.rows) == 300

    second_source = _FakeSource(rows)
    second_target = _FakeTarget()
    second_target.rows = list(first_target.rows)  # whatever landed is still there
    report = _run(_table(), second_source, second_target, shards=4, checkpoint=checkpoint)

    assert sorted(second_target.rows) == rows, "resume must not duplicate or drop rows"
    # Only the one failed shard was re-read; the other three were skipped.
    assert len(second_source.queries) == 1
    assert report.results[0].succeeded is True


def test_resume_validates_using_the_finished_shards_stored_checksums():
    # A skipped shard still has to contribute its checksum, or the
    # combined value would be compared against the target's complete one
    # and report a spurious mismatch.
    rows = _rows(400)
    checkpoint = _checkpoint()
    first_target = _FakeTarget(fail_on_first_id=201)
    _run(_table(), _FakeSource(rows), first_target, shards=4, checkpoint=checkpoint)

    second_target = _FakeTarget()
    second_target.rows = list(first_target.rows)
    report = _run(_table(), _FakeSource(rows), second_target, shards=4, checkpoint=checkpoint)

    validation = report.results[0].validation
    assert validation is not None
    assert validation.checksums_match is True
    assert validation.ok is True


def test_shard_statuses_are_recorded_on_the_checkpoint():
    checkpoint = _checkpoint()
    _run(_table(), _FakeSource(_rows(400)), _FakeTarget(fail_on_first_id=201), shards=4, checkpoint=checkpoint)
    shards = checkpoint.tables["BIG"].shards
    assert set(shards) == {"1of4", "2of4", "3of4", "4of4"}
    assert shards["3of4"].status == "failed"
    assert [shards[k].status for k in ("1of4", "2of4", "4of4")] == ["done", "done", "done"]


def test_shard_checkpoints_survive_a_save_load_round_trip():
    base = pathlib.Path(tempfile.mkdtemp(prefix="tgdatabridge_shard_"))
    checkpoint = _checkpoint()
    _run(
        _table(), _FakeSource(_rows(400)), _FakeTarget(fail_on_first_id=201), shards=4,
        checkpoint=checkpoint, on_checkpoint_update=lambda: app_storage.save_checkpoint(checkpoint, base_dir=base),
    )
    reloaded = app_storage.load_checkpoint("cp-test", base_dir=base)
    assert reloaded is not None
    shards = reloaded.tables["BIG"].shards
    assert shards["3of4"].status == "failed"
    assert shards["1of4"].status == "done"
    assert shards["1of4"].rows_copied > 0
    assert shards["1of4"].checksum != 0


def test_a_fully_completed_sharded_table_is_skipped_on_rerun():
    rows = _rows(400)
    checkpoint = _checkpoint()
    _run(_table(), _FakeSource(rows), _FakeTarget(), shards=4, checkpoint=checkpoint)

    rerun_source = _FakeSource(rows)
    report = _run(_table(), rerun_source, _FakeTarget(), shards=4, checkpoint=checkpoint)
    assert rerun_source.queries == []
    assert report.results[0].skipped is True
