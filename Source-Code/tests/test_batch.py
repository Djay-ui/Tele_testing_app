"""Tests for tgdatabridge.core.batch -- multi-schema/whole-database batch
migration orchestration (ENTERPRISE_READINESS.md section 4, item 3). See
that module's own docstring for why schemas are always run one at a time
(never concurrently with each other) while each schema's own tables can
still be parallelized via its job's own max_workers."""
import threading
import time

from tgdatabridge.core.batch import SchemaMigrationJob, run_batch_migration
from tgdatabridge.core.schema_model import Column, Table


def _table(name):
    return Table(name=name, schema="S", columns=[Column(name=f"{name}_ID", data_type="NUMBER")])


class _FakeConn:
    def __init__(self, rows_by_table=None):
        self.rows_by_table = rows_by_table or {}
        self.closed = False
        self.inserted = []

    def fetch_batches(self, sql, batch_size=5000):
        for name, (cols, rows) in self.rows_by_table.items():
            # Quoted now -- see tgdatabridge.db.source_sql.
            if f'."{name}"' in sql or f".{name}" in sql:
                yield cols, rows
                return
        yield [], []

    def insert_batch(self, table_name, columns, rows):
        self.inserted.append(table_name)

    def close(self):
        self.closed = True


def test_run_batch_migration_runs_every_job_and_aggregates_rows():
    t1 = _table("T1")
    t2 = _table("T2")
    conns = []

    def source_factory(rows):
        def factory():
            c = _FakeConn(rows)
            conns.append(c)
            return c
        return factory

    def target_factory():
        c = _FakeConn()
        conns.append(c)
        return c

    job1 = SchemaMigrationJob(
        schema_name="S1", tables=[t1],
        source_factory=source_factory({"T1": (["T1_ID"], [(1,), (2,)])}),
        target_factory=target_factory,
    )
    job2 = SchemaMigrationJob(
        schema_name="S2", tables=[t2],
        source_factory=source_factory({"T2": (["T2_ID"], [(1,)])}),
        target_factory=target_factory,
    )
    report = run_batch_migration([job1, job2])
    assert set(report.schema_reports.keys()) == {"S1", "S2"}
    assert report.total_rows == 3
    assert report.failed_schemas == []


def test_run_batch_migration_closes_every_job_connection():
    t1 = _table("T1")
    conns = []

    def source_factory():
        c = _FakeConn({"T1": (["T1_ID"], [(1,)])})
        conns.append(c)
        return c

    def target_factory():
        c = _FakeConn()
        conns.append(c)
        return c

    job = SchemaMigrationJob(schema_name="S1", tables=[t1], source_factory=source_factory, target_factory=target_factory)
    run_batch_migration([job])
    assert len(conns) == 2
    assert all(c.closed for c in conns)


def test_run_batch_migration_isolates_one_bad_job_from_the_rest():
    t1 = _table("T1")
    t2 = _table("T2")

    def bad_source_factory():
        raise RuntimeError("cannot connect to source")

    def good_source_factory():
        return _FakeConn({"T2": (["T2_ID"], [(1,)])})

    def target_factory():
        return _FakeConn()

    job1 = SchemaMigrationJob(schema_name="S1", tables=[t1], source_factory=bad_source_factory, target_factory=target_factory)
    job2 = SchemaMigrationJob(schema_name="S2", tables=[t2], source_factory=good_source_factory, target_factory=target_factory)

    report = run_batch_migration([job1, job2])
    assert "S1" in report.schema_errors
    assert "cannot connect to source" in report.schema_errors["S1"]
    assert "S2" in report.schema_reports  # the second job still ran
    assert report.failed_schemas == ["S1"]


def test_run_batch_migration_failed_schemas_includes_partial_table_failures():
    t1 = _table("T1")

    class _FailingTarget(_FakeConn):
        def insert_batch(self, table_name, columns, rows):
            raise RuntimeError("insert failed")

    def source_factory():
        return _FakeConn({"T1": (["T1_ID"], [(1,)])})

    def target_factory():
        return _FailingTarget()

    job = SchemaMigrationJob(schema_name="S1", tables=[t1], source_factory=source_factory, target_factory=target_factory)
    report = run_batch_migration([job])
    assert "S1" in report.schema_reports  # migrate_schema itself didn't raise...
    assert report.schema_reports["S1"].failed_tables == ["T1"]  # ...but the table inside it failed
    assert report.failed_schemas == ["S1"]


def test_run_batch_migration_reports_batch_level_progress():
    t1 = _table("T1")
    t2 = _table("T2")

    def source_factory():
        return _FakeConn({"T1": (["T1_ID"], [(1,)]), "T2": (["T2_ID"], [(1,)])})

    def target_factory():
        return _FakeConn()

    job1 = SchemaMigrationJob(schema_name="S1", tables=[t1], source_factory=source_factory, target_factory=target_factory)
    job2 = SchemaMigrationJob(schema_name="S2", tables=[t2], source_factory=source_factory, target_factory=target_factory)
    calls = []
    run_batch_migration([job1, job2], schema_progress_cb=lambda done, total: calls.append((done, total)))
    assert calls == [(1, 2), (2, 2)]


def test_run_batch_migration_schemas_never_run_concurrently_with_each_other():
    # Even when each job's own max_workers > 1 (parallelizing that
    # schema's own tables), one schema's job must fully finish before the
    # next schema's job starts -- see this module's own docstring for why.
    events = []
    lock = threading.Lock()

    class _SlowConn(_FakeConn):
        def fetch_batches(self, sql, batch_size=5000):
            time.sleep(0.03)
            yield from super().fetch_batches(sql, batch_size)

        def insert_batch(self, table_name, columns, rows):
            with lock:
                events.append((table_name, "start", time.time()))
            time.sleep(0.03)
            with lock:
                events.append((table_name, "end", time.time()))

    def source_factory(rows):
        def factory():
            return _SlowConn(rows)
        return factory

    def target_factory():
        return _SlowConn()

    t1 = _table("T1")
    t2 = _table("T2")
    job1 = SchemaMigrationJob(
        schema_name="S1", tables=[t1], max_workers=2,
        source_factory=source_factory({"T1": (["T1_ID"], [(1,)])}), target_factory=target_factory,
    )
    job2 = SchemaMigrationJob(
        schema_name="S2", tables=[t2], max_workers=2,
        source_factory=source_factory({"T2": (["T2_ID"], [(1,)])}), target_factory=target_factory,
    )
    run_batch_migration([job1, job2])

    t1_end = next(t for (name, kind, t) in events if name == "T1" and kind == "end")
    t2_start = next(t for (name, kind, t) in events if name == "T2" and kind == "start")
    assert t1_end <= t2_start
