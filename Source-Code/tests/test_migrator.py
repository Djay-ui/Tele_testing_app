"""Tests for tgdatabridge.core.migrator: FK-dependency ordering for data migration,
and that a per-table failure is captured with its actual error text (not
just the table name) so the GUI has something to surface.

order_tables_by_dependency exists because, unlike DDL generation (which can
defer every FK to a second pass run only after all tables exist), data
migration runs *after* Apply DDL has already created live FK constraints on
the target -- so migrating a child table before its parent's rows are
loaded fails immediately with a foreign key violation on the child's first
batch. This is exactly what happened with "Failed tables: ACCOUNT,
TRANSACTION_HISTORY": both are child tables (ACCOUNT -> CUSTOMER,
TRANSACTION_HISTORY -> ACCOUNT) migrated out of dependency order.

The "parallel table migration" and "LOB-aware batch sizing" sections below
cover ENTERPRISE_READINESS.md section 4's scale/performance work -- see
migrate_schema's and _table_has_lob_columns's own docstrings.
"""
import threading
import time

from tgdatabridge.core import migrator
from tgdatabridge.core.schema_model import Constraint, Column, Table


def _table(name, fk_to=None):
    constraints = [Constraint(name=f"PK_{name}", kind="PRIMARY KEY", columns=[f"{name}_ID"])]
    if fk_to:
        constraints.append(Constraint(
            name=f"FK_{name}_{fk_to}", kind="FOREIGN KEY",
            columns=[f"{fk_to}_ID"], ref_table=fk_to, ref_columns=[f"{fk_to}_ID"],
        ))
    return Table(name=name, schema="HR", columns=[Column(name=f"{name}_ID", data_type="NUMBER")],
                 constraints=constraints)


def test_order_tables_by_dependency_puts_parent_before_child():
    account = _table("ACCOUNT", fk_to="CUSTOMER")
    customer = _table("CUSTOMER")
    ordered = migrator.order_tables_by_dependency([account, customer])
    names = [t.name for t in ordered]
    assert names.index("CUSTOMER") < names.index("ACCOUNT")


def test_order_tables_by_dependency_handles_multi_level_chain_regardless_of_input_order():
    # Reproduces the exact real-world failure: ACCOUNT -> CUSTOMER and
    # TRANSACTION_HISTORY -> ACCOUNT, listed in an order that puts both
    # children before (or interleaved with) their parents.
    transaction_history = _table("TRANSACTION_HISTORY", fk_to="ACCOUNT")
    account = _table("ACCOUNT", fk_to="CUSTOMER")
    customer = _table("CUSTOMER")
    ordered = migrator.order_tables_by_dependency([transaction_history, account, customer])
    names = [t.name for t in ordered]
    assert names.index("CUSTOMER") < names.index("ACCOUNT") < names.index("TRANSACTION_HISTORY")


def test_order_tables_by_dependency_tolerates_cycles_without_hanging():
    # Two tables that reference each other: no valid total order exists.
    # Must not infinite-loop; falls back to appending the remaining (cyclic)
    # tables rather than dropping them.
    a = _table("A", fk_to="B")
    b = _table("B", fk_to="A")
    ordered = migrator.order_tables_by_dependency([a, b])
    assert {t.name for t in ordered} == {"A", "B"}


def test_order_tables_by_dependency_ignores_fk_to_table_outside_the_list():
    # A FK referencing a table that isn't part of this migration run (e.g.
    # only a subset of tables was selected) shouldn't block ordering.
    account = _table("ACCOUNT", fk_to="CUSTOMER")  # CUSTOMER not included
    ordered = migrator.order_tables_by_dependency([account])
    assert [t.name for t in ordered] == ["ACCOUNT"]


class _FakeSource:
    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table

    def fetch_batches(self, sql, batch_size=5000):
        for table_name, (columns, rows) in self.rows_by_table.items():
            # The table name now arrives quoted -- see tgdatabridge.db.source_sql.
            if f'."{table_name}"' in sql or f".{table_name}" in sql:
                yield columns, rows
                return
        yield [], []


class _FakeTarget:
    def __init__(self, fail_tables=None):
        self.fail_tables = fail_tables or set()
        self.inserted_order = []

    def insert_batch(self, table_name, columns, rows):
        if table_name in self.fail_tables:
            raise RuntimeError(f"insert or update on table \"{table_name.lower()}\" violates foreign key constraint")
        self.inserted_order.append(table_name)


def test_migrate_schema_migrates_parent_before_child():
    account = _table("ACCOUNT", fk_to="CUSTOMER")
    customer = _table("CUSTOMER")
    source = _FakeSource({
        "ACCOUNT": (["ACCOUNT_ID"], [(1,)]),
        "CUSTOMER": (["CUSTOMER_ID"], [(1,)]),
    })
    target = _FakeTarget()
    report = migrator.migrate_schema(source, target, [account, customer])
    assert target.inserted_order.index("CUSTOMER") < target.inserted_order.index("ACCOUNT")
    assert report.failed_tables == []


def test_migrate_schema_reports_table_level_progress():
    # On a schema with thousands of tables, table_progress_cb is what lets
    # the GUI show a real N/total instead of an indeterminate spinner for
    # the whole "Migrate Data" run.
    account = _table("ACCOUNT", fk_to="CUSTOMER")
    customer = _table("CUSTOMER")
    source = _FakeSource({
        "ACCOUNT": (["ACCOUNT_ID"], [(1,)]),
        "CUSTOMER": (["CUSTOMER_ID"], [(1,)]),
    })
    target = _FakeTarget()
    calls = []
    migrator.migrate_schema(
        source, target, [account, customer],
        table_progress_cb=lambda done, total: calls.append((done, total)))
    assert calls == [(1, 2), (2, 2)]


class _FakeTableSource:
    """Mimics MongoConnector's fetch_batches_table -- a source connector
    that defines *this* (not fetch_batches) must be preferred by
    migrate_table's duck-typed dispatch, without ever building/using a
    SQL string."""

    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table
        self.tables_requested = []

    def fetch_batches_table(self, table, batch_size=5000):
        self.tables_requested.append(table.name)
        columns, rows = self.rows_by_table.get(table.name, ([], []))
        yield columns, rows

    def fetch_batches(self, sql, batch_size=5000):  # pragma: no cover - must never be called
        raise AssertionError("fetch_batches (SQL-string path) must not be used when fetch_batches_table exists")


def test_migrate_table_prefers_fetch_batches_table_when_present():
    account = _table("ACCOUNT")
    source = _FakeTableSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _FakeTarget()
    result = migrator.migrate_table(source, target, account)
    assert result.succeeded is True
    assert result.rows_copied == 1
    assert source.tables_requested == ["ACCOUNT"]
    assert target.inserted_order == ["ACCOUNT"]


def test_migrate_table_falls_back_to_sql_string_fetch_batches_when_no_table_method():
    # Regression guard: every existing (non-Mongo) source connector has no
    # fetch_batches_table attribute at all, so hasattr(...) must be False
    # and this exact pre-existing SQL-string path must still run unchanged.
    account = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _FakeTarget()
    result = migrator.migrate_table(source, target, account)
    assert result.succeeded is True
    assert result.rows_copied == 1


def test_migrate_table_captures_the_actual_error_message():
    # MigrationResult.error must carry the real exception text -- the GUI
    # previously discarded this and only ever logged the failing table's
    # name, leaving no way to see *why* it failed.
    account = _table("ACCOUNT", fk_to="CUSTOMER")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _FakeTarget(fail_tables={"ACCOUNT"})
    result = migrator.migrate_table(source, target, account)
    assert result.succeeded is False
    assert "foreign key constraint" in result.error


# ------------------------------------------------------- post-migration validation


class _ValidatingTarget(_FakeTarget):
    """Adds count_rows()/checksum_rows()/schema_name -- the trio
    tgdatabridge.core.validation.validate_table looks for via getattr(...,
    None) -- on top of _FakeTarget's insert_batch. Row counts/checksums
    are computed from whatever was actually inserted, so a genuinely
    wrong/dropped row is caught the same way a real target driver bug
    would be."""

    schema_name = "HR"

    def __init__(self, fail_tables=None, drop_rows_for=None):
        super().__init__(fail_tables)
        self.rows_by_table: dict = {}
        self.drop_rows_for = drop_rows_for or set()

    def insert_batch(self, table_name, columns, rows):
        super().insert_batch(table_name, columns, rows)
        if table_name in self.drop_rows_for:
            return  # simulate a target that silently drops the rows
        self.rows_by_table.setdefault(table_name, []).extend(rows)

    def count_rows(self, table_name, schema=None):
        return len(self.rows_by_table.get(table_name, []))

    def checksum_rows(self, table_name, columns, schema=None, sample_size=None):
        from tgdatabridge.core.validation import table_checksum
        return table_checksum(self.rows_by_table.get(table_name, []))


def test_migrate_table_validation_passes_when_target_matches():
    account = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,), (2,)])})
    target = _ValidatingTarget()
    result = migrator.migrate_table(source, target, account)
    assert result.succeeded is True
    assert result.validation is not None
    assert result.validation.ok is True
    assert result.validation.row_counts_match is True
    assert result.validation.checksums_match is True


def test_migrate_table_validation_flags_row_count_mismatch():
    # Target silently drops every row it's given -- rows_copied still
    # reports what was sent, but validation must catch that none of it
    # actually landed.
    account = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,), (2,)])})
    target = _ValidatingTarget(drop_rows_for={"ACCOUNT"})
    result = migrator.migrate_table(source, target, account)
    assert result.succeeded is True  # insert_batch itself didn't raise
    assert result.rows_copied == 2
    assert result.validation.row_counts_match is False
    assert result.validation.ok is False


def test_migrate_table_validate_false_skips_validation_entirely():
    account = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _ValidatingTarget(drop_rows_for={"ACCOUNT"})
    result = migrator.migrate_table(source, target, account, validate=False)
    assert result.validation is None


def test_migration_report_unvalidated_tables_excludes_skipped_and_clean_tables():
    account = _table("ACCOUNT")
    customer = _table("CUSTOMER")
    source = _FakeSource({
        "ACCOUNT": (["ACCOUNT_ID"], [(1,)]),
        "CUSTOMER": (["CUSTOMER_ID"], [(1,)]),
    })
    target = _ValidatingTarget(drop_rows_for={"ACCOUNT"})
    report = migrator.migrate_schema(source, target, [account, customer])
    assert report.unvalidated_tables == ["ACCOUNT"]


# ------------------------------------------------------------- retry with backoff


class _FlakyTarget(_FakeTarget):
    """Fails insert_batch for a table a fixed number of times (a
    "connection reset"-flavored message, so retry.is_transient treats it
    as retryable) before succeeding."""

    def __init__(self, fail_first_n: int):
        super().__init__()
        self.fail_first_n = fail_first_n
        self.attempts = 0

    def insert_batch(self, table_name, columns, rows):
        self.attempts += 1
        if self.attempts <= self.fail_first_n:
            raise RuntimeError("connection reset by peer")
        self.inserted_order.append(table_name)


def test_migrate_table_retries_transient_failures_and_eventually_succeeds():
    from tgdatabridge.core.retry import RetryPolicy

    account = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _FlakyTarget(fail_first_n=2)
    result = migrator.migrate_table(
        source, target, account,
        retry_policy=RetryPolicy(max_attempts=3, base_delay=0, max_delay=0),
    )
    assert result.succeeded is True
    assert target.attempts == 3


def test_migrate_table_gives_up_after_max_attempts():
    from tgdatabridge.core.retry import RetryPolicy

    account = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _FlakyTarget(fail_first_n=5)
    result = migrator.migrate_table(
        source, target, account,
        retry_policy=RetryPolicy(max_attempts=2, base_delay=0, max_delay=0),
    )
    assert result.succeeded is False
    assert target.attempts == 2


def test_migrate_table_calls_on_retry_callback():
    from tgdatabridge.core.retry import RetryPolicy

    account = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _FlakyTarget(fail_first_n=1)
    calls = []
    migrator.migrate_table(
        source, target, account,
        retry_policy=RetryPolicy(max_attempts=3, base_delay=0, max_delay=0),
        on_retry=lambda table_name, attempt, exc, delay: calls.append((table_name, attempt)),
    )
    assert calls == [("ACCOUNT", 1)]


class _ReconnectableFlakyTarget(_FlakyTarget):
    """Adds connect()/close() tracking on top of _FlakyTarget's
    fail-then-succeed insert_batch, so a test can confirm migrate_table's
    retry actually reconnects a real connector-shaped target -- not just
    that it retries the call. Real usage is via a `target_factory` a
    caller passed to `migrate_schema`, so its connection is fully live
    (already connect()-ed once) by the time migrate_table ever sees it;
    this fake reflects that starting state."""

    def __init__(self, fail_first_n: int):
        super().__init__(fail_first_n)
        self.connect_calls = 0
        self.close_calls = 0

    def connect(self):
        self.connect_calls += 1

    def close(self):
        self.close_calls += 1


def test_migrate_table_reconnects_the_target_before_retrying_insert_batch():
    # The actual bug this fixes: a connection genuinely closed by the
    # network or database ("DPY-4011: the database or network closed the
    # connection", on a real 8+ hour production migration) failed every
    # retry identically, because nothing re-established the connection
    # itself -- retrying just resent the same call to the same dead
    # connection object.
    from tgdatabridge.core.retry import RetryPolicy

    account = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _ReconnectableFlakyTarget(fail_first_n=2)
    result = migrator.migrate_table(
        source, target, account,
        retry_policy=RetryPolicy(max_attempts=3, base_delay=0, max_delay=0),
    )
    assert result.succeeded is True
    # One reconnect per retry (2 failures -> 2 reconnects), each a
    # matched close() then connect() -- never connect() without a prior
    # close() of the dead connection.
    assert target.close_calls == target.connect_calls == 2


def test_migrate_table_does_not_reconnect_a_target_without_connect_and_close():
    # This tool's own lighter test doubles (and any future connector that
    # manages its connection some other way) must not break just because
    # they don't duck-type connect()/close() -- migrate_table falls back
    # to a plain retry for them, exactly as it always has.
    from tgdatabridge.core.retry import RetryPolicy

    account = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _FlakyTarget(fail_first_n=1)  # no connect()/close() at all
    result = migrator.migrate_table(
        source, target, account,
        retry_policy=RetryPolicy(max_attempts=3, base_delay=0, max_delay=0),
    )
    assert result.succeeded is True  # unaffected: still retries, just doesn't reconnect


def test_migrate_table_without_retry_policy_fails_immediately_as_before():
    # Regression guard: no retry_policy given must behave exactly like the
    # pre-retry implementation -- one attempt, immediate failure.
    account = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _FlakyTarget(fail_first_n=1)
    result = migrator.migrate_table(source, target, account)
    assert result.succeeded is False
    assert target.attempts == 1


# -------------------------------------------------- source-connection reconnect


class _DisconnectingSource:
    """Simulates a source whose connection drops mid-fetch -- the exact
    "DPY-4011: the database or network closed the connection" a real
    migration hit while streaming a LOB-heavy table. `fetch_batches`
    raises that error, after yielding `fail_after` batches, on its
    *first* call only; every call made while `connected` is False (i.e.
    before migrate_table's reconnect runs) instead raises the exact
    follow-on error a real dead Oracle connection produces
    ("DPY-1001: not connected to database"), so a test can prove a
    reconnect actually happened rather than merely that a retry was
    attempted."""

    def __init__(self, batches, fail_after):
        self.batches = batches
        self.fail_after = fail_after
        self.attempt = 0
        self.connected = True
        self.connect_calls = 0
        self.close_calls = 0

    def connect(self):
        self.connect_calls += 1
        self.connected = True

    def close(self):
        self.close_calls += 1
        self.connected = False

    def fetch_batches(self, sql, batch_size=5000):
        self.attempt += 1
        if not self.connected:
            raise RuntimeError("DPY-1001: not connected to database")
        for index, (columns, rows) in enumerate(self.batches, start=1):
            if self.attempt == 1 and index > self.fail_after:
                raise RuntimeError("DPY-4011: the database or network closed the connection")
            yield columns, rows


def test_migrate_table_reconnects_the_source_and_resumes_after_a_dropped_connection():
    from tgdatabridge.utils.app_storage import TableCheckpoint
    from tgdatabridge.core.retry import RetryPolicy

    account = _table("ACCOUNT")
    source = _DisconnectingSource(
        batches=[
            (["ACCOUNT_ID"], [(1,)]),
            (["ACCOUNT_ID"], [(2,)]),
            (["ACCOUNT_ID"], [(3,)]),
        ],
        fail_after=1,  # yields batch 1 successfully, then drops before batch 2
    )
    target = _FakeTarget()
    checkpoint = TableCheckpoint()

    result = migrator.migrate_table(
        source, target, account, checkpoint=checkpoint,
        retry_policy=RetryPolicy(max_attempts=3, base_delay=0, max_delay=0),
    )

    assert result.succeeded is True
    assert result.rows_copied == 3
    # Batch 1 landed before the drop; the retry re-reads from the top
    # (a fresh SELECT on the reconnected source has no way to "resume"
    # a partial read), but the checkpoint recognizes batch 1 as already
    # written and only batches 2 and 3 are inserted a second time.
    assert target.inserted_order == ["ACCOUNT", "ACCOUNT", "ACCOUNT"]
    assert checkpoint.status == "done"
    # Exactly one reconnect -- for the one connection drop.
    assert source.close_calls == source.connect_calls == 1


def test_migrate_table_without_checkpoint_does_not_retry_the_source_read():
    # Retrying a source read from scratch without a checkpoint would
    # re-insert whatever the failed attempt already wrote -- unlike the
    # target-side insert retry (a single batch, safe to resend), there is
    # no portable OFFSET to skip past what already landed. So this table
    # still fails outright, exactly as it did before this feature existed.
    from tgdatabridge.core.retry import RetryPolicy

    account = _table("ACCOUNT")
    source = _DisconnectingSource(
        batches=[(["ACCOUNT_ID"], [(1,)]), (["ACCOUNT_ID"], [(2,)])],
        fail_after=0,  # drops before yielding anything
    )
    target = _FakeTarget()

    result = migrator.migrate_table(
        source, target, account,
        retry_policy=RetryPolicy(max_attempts=3, base_delay=0, max_delay=0),
    )

    assert result.succeeded is False
    assert "DPY-4011" in result.error


def test_migrate_table_reconnects_the_source_even_without_a_checkpoint():
    # This is the fix for the *other* real-world symptom: even when this
    # table's own read can't safely be retried (no checkpoint), the
    # source connection must still be healed before migrate_table
    # returns -- otherwise the very next table migrated with this same
    # source object inherits a dead connection and fails instantly with
    # "DPY-1001: not connected to database", which is what actually
    # happened to 17 tables in a row on a real migration.
    account = _table("ACCOUNT")
    source = _DisconnectingSource(
        batches=[(["ACCOUNT_ID"], [(1,)])],
        fail_after=0,
    )
    target = _FakeTarget()

    result = migrator.migrate_table(source, target, account)

    assert result.succeeded is False
    assert source.close_calls == source.connect_calls == 1


def test_migrate_schema_does_not_cascade_a_source_drop_into_every_later_table():
    # End-to-end version of the fix above, through migrate_schema's own
    # single-threaded loop (which reuses the same `source` object across
    # every table, exactly like the real GUI's "Migrate Data" run): a
    # connection drop on one table must not fail every table after it.
    account = _table("ACCOUNT")
    customer = _table("CUSTOMER")

    class _PerTableDisconnectingSource:
        def __init__(self, rows_by_table, fail_once_table):
            self.rows_by_table = rows_by_table
            self.fail_once_table = fail_once_table
            self._already_failed = False
            self.connected = True
            self.connect_calls = 0
            self.close_calls = 0

        def connect(self):
            self.connect_calls += 1
            self.connected = True

        def close(self):
            self.close_calls += 1
            self.connected = False

        def fetch_batches(self, sql, batch_size=5000):
            if not self.connected:
                raise RuntimeError("DPY-1001: not connected to database")
            for table_name, (columns, rows) in self.rows_by_table.items():
                if f'."{table_name}"' in sql or f".{table_name}" in sql:
                    if table_name == self.fail_once_table and not self._already_failed:
                        self._already_failed = True
                        raise RuntimeError(
                            "DPY-4011: the database or network closed the connection")
                    yield columns, rows
                    return
            yield [], []

    source = _PerTableDisconnectingSource(
        rows_by_table={
            "ACCOUNT": (["ACCOUNT_ID"], [(1,)]),
            "CUSTOMER": (["CUSTOMER_ID"], [(1,)]),
        },
        fail_once_table="ACCOUNT",
    )
    target = _FakeTarget()

    report = migrator.migrate_schema(source, target, [account, customer])

    account_result = next(r for r in report.results if r.table_name == "ACCOUNT")
    customer_result = next(r for r in report.results if r.table_name == "CUSTOMER")
    assert account_result.succeeded is False
    # The whole point: CUSTOMER succeeds because ACCOUNT's failure
    # reconnected the shared source connection before migrate_schema
    # moved on -- without that fix this would also fail, with
    # "DPY-1001: not connected to database".
    assert customer_result.succeeded is True
    assert source.connect_calls == 1


# ------------------------------------------------------------ checkpoint/resume


def _checkpoint(schema_name="HR", source_engine="Oracle", target_engine="PostgreSQL"):
    from tgdatabridge.utils.app_storage import MigrationCheckpoint
    return MigrationCheckpoint(
        checkpoint_id="test-checkpoint", schema_name=schema_name,
        source_engine=source_engine, target_engine=target_engine,
    )


def test_migrate_schema_skips_table_already_marked_done_on_checkpoint():
    from tgdatabridge.utils.app_storage import TableCheckpoint

    account = _table("ACCOUNT")
    customer = _table("CUSTOMER")
    source = _FakeSource({
        "ACCOUNT": (["ACCOUNT_ID"], [(1,)]),
        "CUSTOMER": (["CUSTOMER_ID"], [(1,)]),
    })
    target = _FakeTarget()
    checkpoint = _checkpoint()
    checkpoint.tables["CUSTOMER"] = TableCheckpoint(status="done", rows_copied=99, batches_completed=1)

    report = migrator.migrate_schema(source, target, [account, customer], checkpoint=checkpoint)

    customer_result = next(r for r in report.results if r.table_name == "CUSTOMER")
    assert customer_result.skipped is True
    assert customer_result.rows_copied == 99
    assert "CUSTOMER" not in target.inserted_order  # never re-written


def test_migrate_schema_marks_table_done_on_checkpoint_after_success():
    account = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _FakeTarget()
    checkpoint = _checkpoint()
    migrator.migrate_schema(source, target, [account], checkpoint=checkpoint)
    assert checkpoint.tables["ACCOUNT"].status == "done"
    assert checkpoint.all_done is True


def test_migrate_schema_marks_table_failed_on_checkpoint():
    account = _table("ACCOUNT", fk_to="CUSTOMER")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _FakeTarget(fail_tables={"ACCOUNT"})
    checkpoint = _checkpoint()
    migrator.migrate_schema(source, target, [account], checkpoint=checkpoint)
    assert checkpoint.tables["ACCOUNT"].status == "failed"
    assert checkpoint.has_failures is True


def test_migrate_schema_calls_on_checkpoint_update():
    account = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _FakeTarget()
    checkpoint = _checkpoint()
    calls = []
    migrator.migrate_schema(
        source, target, [account], checkpoint=checkpoint,
        on_checkpoint_update=lambda: calls.append(checkpoint.tables["ACCOUNT"].status),
    )
    assert "in_progress" in calls or "done" in calls
    assert calls[-1] == "done"


class _MultiBatchTableSource:
    """Yields more than one batch for one table -- lets a resume test
    verify migrate_table only skips the batches already known to be on
    the target, not the whole table."""

    def __init__(self, batches):
        self.batches = batches
        self.calls = 0

    def fetch_batches(self, sql, batch_size=5000):
        self.calls += 1
        for columns, rows in self.batches:
            yield columns, rows


def test_migrate_table_resumes_by_skipping_already_written_batches():
    from tgdatabridge.utils.app_storage import TableCheckpoint

    account = _table("ACCOUNT")
    source = _MultiBatchTableSource([
        (["ACCOUNT_ID"], [(1,)]),
        (["ACCOUNT_ID"], [(2,)]),
        (["ACCOUNT_ID"], [(3,)]),
    ])
    target = _FakeTarget()
    # Simulate a previous run that got through the first batch (1 row)
    # before failing.
    checkpoint = TableCheckpoint(status="in_progress", rows_copied=1, batches_completed=1)

    result = migrator.migrate_table(source, target, account, checkpoint=checkpoint)

    assert result.succeeded is True
    # Only batches 2 and 3 (rows 2 and 3) are actually inserted -- batch 1
    # (row 1) is assumed already on the target from the previous run.
    assert target.inserted_order == ["ACCOUNT", "ACCOUNT"]
    # rows_copied continues from the checkpoint's prior total, not from 0.
    assert result.rows_copied == 3
    assert checkpoint.status == "done"


def test_migrate_table_resume_checksum_matches_a_fresh_uninterrupted_run():
    # The final checksum after a resumed run must equal what a single,
    # uninterrupted run over the same rows would have produced -- resuming
    # is an implementation detail, not something that should change what
    # validation considers "correct".
    account = _table("ACCOUNT")

    def make_source():
        return _MultiBatchTableSource([
            (["ACCOUNT_ID"], [(1,)]),
            (["ACCOUNT_ID"], [(2,)]),
        ])

    fresh_target = _ValidatingTarget()
    fresh_result = migrator.migrate_table(make_source(), fresh_target, account)

    from tgdatabridge.utils.app_storage import TableCheckpoint
    resumed_target = _ValidatingTarget()
    # Pre-populate the target with batch 1's row, as if a previous run had
    # already written it, and start the checkpoint after that batch.
    resumed_target.rows_by_table["ACCOUNT"] = [(1,)]
    checkpoint = TableCheckpoint(status="in_progress", rows_copied=1, batches_completed=1)
    resumed_result = migrator.migrate_table(make_source(), resumed_target, account, checkpoint=checkpoint)

    assert resumed_result.rows_copied == fresh_result.rows_copied == 2
    assert resumed_result.validation.checksums_match is True
    assert fresh_result.validation.checksums_match is True


# ------------------------------------------- deterministic resume ordering


class _SqlCapturingSource(_MultiBatchTableSource):
    """Like _MultiBatchTableSource, but keeps the exact SQL string
    migrate_table built, so a test can assert on its shape (e.g. that it
    does or doesn't carry an ORDER BY) without caring what rows come
    back."""

    def __init__(self, batches):
        super().__init__(batches)
        self.last_sql = None

    def fetch_batches(self, sql, batch_size=5000):
        self.last_sql = sql
        return super().fetch_batches(sql, batch_size=batch_size)


def test_migrate_table_orders_the_select_by_the_single_column_primary_key():
    # See migrator._resume_order_columns' own docstring: without this, a
    # resumed migration's batch-skip logic can silently duplicate or drop
    # rows, because Oracle (and every other engine here) makes no promise
    # that re-running the same bare SELECT returns rows in the same order.
    account = _table("ACCOUNT")  # _table() gives every table a single-column PK
    source = _SqlCapturingSource([(["ACCOUNT_ID"], [(1,)])])
    migrator.migrate_table(source, _FakeTarget(), account)
    assert 'ORDER BY "ACCOUNT_ID"' in source.last_sql


def test_migrate_table_orders_by_every_column_of_a_composite_primary_key():
    from tgdatabridge.core.schema_model import Column, Constraint, Table
    table = Table(
        name="ORDER_ITEMS", schema="HR",
        columns=[Column(name="ORDER_ID", data_type="NUMBER"), Column(name="LINE_NO", data_type="NUMBER")],
        constraints=[Constraint(name="PK_ORDER_ITEMS", kind="PRIMARY KEY", columns=["ORDER_ID", "LINE_NO"])],
    )
    source = _SqlCapturingSource([(["ORDER_ID", "LINE_NO"], [(1, 1)])])
    migrator.migrate_table(source, _FakeTarget(), table)
    assert 'ORDER BY "ORDER_ID", "LINE_NO"' in source.last_sql


def test_migrate_table_adds_no_order_by_for_a_table_without_a_primary_key():
    # Preserves the pre-existing behavior for a table this tool has no
    # safe, guaranteed-unique column set to order by -- see
    # _resume_order_columns' own docstring on why it refuses to guess.
    from tgdatabridge.core.schema_model import Column, Table
    table = Table(name="STAGING", schema="HR", columns=[Column(name="ACCOUNT_ID", data_type="NUMBER")])
    source = _SqlCapturingSource([(["ACCOUNT_ID"], [(1,)])])
    migrator.migrate_table(source, _FakeTarget(), table)
    assert "ORDER BY" not in source.last_sql


def test_migrate_table_skips_order_by_when_the_pk_column_is_not_being_migrated():
    # Defensive: a primary key column this migration isn't actually
    # selecting (should not happen in practice, but costs nothing to
    # guard) must not be named in an ORDER BY -- that's a query error on
    # every engine here, not a resume-safety improvement.
    from tgdatabridge.core.schema_model import Column, Constraint, Table
    table = Table(
        name="ACCOUNT", schema="HR", columns=[Column(name="NAME", data_type="VARCHAR2(50)")],
        constraints=[Constraint(name="PK_ACCOUNT", kind="PRIMARY KEY", columns=["ACCOUNT_ID"])],
    )
    source = _SqlCapturingSource([(["NAME"], [("a",)])])
    migrator.migrate_table(source, _FakeTarget(), table)
    assert "ORDER BY" not in source.last_sql


class _UnstableOrderSource:
    """Mimics what a real production run hit: a bare `SELECT ... FROM
    table` with no ORDER BY has no defined row order on Oracle (or any
    other engine here), so a resumed migration re-running that exact
    query can get its rows back in a different order than the run that
    originally populated the checkpoint this resume trusts. This fake
    reproduces that precisely -- honoring an ORDER BY when the SQL
    it's given has one (sorting ascending by the row's first column,
    exactly like a real engine would), and returning the rows reversed
    otherwise, standing in for "some other real scan order"."""

    def __init__(self, rows):
        self.rows = rows

    def fetch_batches(self, sql, batch_size=5000):
        ordered = sorted(self.rows, key=lambda r: r[0]) if "ORDER BY" in sql else list(reversed(self.rows))
        for i in range(0, len(ordered), batch_size):
            yield ["ACCOUNT_ID"], ordered[i:i + batch_size]


def test_resuming_a_table_with_a_primary_key_survives_unstable_row_order():
    # The actual bug, reproduced: a table with 6 rows, migrated 3 at a
    # time. An earlier attempt (not replayed here -- its outcome is what
    # the pre-seeded target and checkpoint below represent) wrote rows
    # 1-3, in ascending order, before failing. This test's resumed run
    # hits a source that -- absent the fix -- would hand back a
    # completely different row order, exactly the scenario that produced
    # both a doubled table and a short table in the same real production
    # run this is from.
    from tgdatabridge.utils.app_storage import TableCheckpoint

    account = _table("ACCOUNT")  # has a primary key -- migrate_table now orders by it
    rows = [(i,) for i in range(1, 7)]
    source = _UnstableOrderSource(rows)
    target = _ValidatingTarget()
    target.rows_by_table["ACCOUNT"] = [(1,), (2,), (3,)]  # what the earlier attempt actually wrote
    checkpoint = TableCheckpoint(status="in_progress", rows_copied=3, batches_completed=1)

    result = migrator.migrate_table(source, target, account, batch_size=3, checkpoint=checkpoint)

    assert result.succeeded is True
    # Exactly the six rows, no duplicates, nothing missing -- the ORDER BY
    # makes this resumed read line up with the earlier attempt's, so batch
    # 1 (rows 1-3, already on the target) is correctly recognized and
    # skipped, and only the genuinely-missing batch 2 (rows 4-6) is sent.
    assert sorted(target.rows_by_table["ACCOUNT"]) == rows


def test_resuming_a_table_without_a_primary_key_can_still_be_corrupted_by_unstable_row_order():
    # Contrast case, pinning the boundary of the fix above rather than a
    # new bug: a table this tool has no safe column set to order by (see
    # _resume_order_columns) is still exposed to exactly the corruption
    # the previous test proves is now fixed for a table *with* a primary
    # key. Same fake source, same checkpoint shape, same pre-seeded
    # target state -- only the table's constraints differ.
    from tgdatabridge.core.schema_model import Column, Table
    from tgdatabridge.utils.app_storage import TableCheckpoint

    account = Table(name="ACCOUNT", schema="HR", columns=[Column(name="ACCOUNT_ID", data_type="NUMBER")])
    rows = [(i,) for i in range(1, 7)]
    source = _UnstableOrderSource(rows)
    target = _ValidatingTarget()
    target.rows_by_table["ACCOUNT"] = [(1,), (2,), (3,)]
    checkpoint = TableCheckpoint(status="in_progress", rows_copied=3, batches_completed=1)

    migrator.migrate_table(source, target, account, batch_size=3, checkpoint=checkpoint)

    # Without an ORDER BY, this resumed read comes back reversed (6,5,4),
    # (3,2,1); batch 1 -- rows 6,5,4 -- is wrongly assumed already-written
    # and skipped, so rows 4-6 never reach the target at all, while batch
    # 2 -- rows 3,2,1 -- gets inserted on top of the already-present 1,2,3,
    # duplicating them. The result is neither a superset nor a subset of
    # the correct six rows.
    assert sorted(target.rows_by_table["ACCOUNT"]) != rows


# ---------------------------------------------------------------- dry-run / plan


class _PlanSource(_FakeSource):
    def __init__(self, rows_by_table, counts=None):
        super().__init__(rows_by_table)
        self.counts = counts or {}

    def count_rows(self, table_name, schema=None):
        return self.counts[table_name]


class _PlanTarget:
    schema_name = "HR"

    def __init__(self, existing_counts=None, missing=None):
        self.existing_counts = existing_counts or {}
        self.missing = missing or set()

    def count_rows(self, table_name, schema=None):
        if table_name in self.missing:
            raise RuntimeError(f'relation "{table_name.lower()}" does not exist')
        return self.existing_counts.get(table_name, 0)


def test_plan_table_reports_source_rows_and_target_readiness():
    account = _table("ACCOUNT")
    source = _PlanSource({}, counts={"ACCOUNT": 150})
    target = _PlanTarget(existing_counts={"ACCOUNT": 0})
    plan = migrator.plan_table(source, target, account)
    assert plan.source_rows == 150
    assert plan.target_exists is True
    assert plan.target_rows_before == 0
    assert plan.ready is True


def test_plan_table_flags_missing_target_table_as_not_ready():
    account = _table("ACCOUNT")
    source = _PlanSource({}, counts={"ACCOUNT": 10})
    target = _PlanTarget(missing={"ACCOUNT"})
    plan = migrator.plan_table(source, target, account)
    assert plan.target_exists is False
    assert plan.ready is False


def test_plan_table_warns_when_target_already_has_rows():
    account = _table("ACCOUNT")
    source = _PlanSource({}, counts={"ACCOUNT": 10})
    target = _PlanTarget(existing_counts={"ACCOUNT": 5})
    plan = migrator.plan_table(source, target, account)
    assert any("already has 5 row" in w for w in plan.warnings)


def test_plan_table_writes_nothing_to_either_side():
    # No insert_batch/execute_ddl-shaped attribute exists on these fakes at
    # all -- plan_table calling anything beyond count_rows would raise
    # AttributeError, so a clean run here is itself proof nothing was
    # written.
    account = _table("ACCOUNT")
    source = _PlanSource({}, counts={"ACCOUNT": 1})
    target = _PlanTarget(existing_counts={"ACCOUNT": 0})
    plan = migrator.plan_table(source, target, account)
    assert plan.error is None


def test_plan_schema_orders_by_dependency_and_totals_source_rows():
    account = _table("ACCOUNT", fk_to="CUSTOMER")
    customer = _table("CUSTOMER")
    source = _PlanSource({}, counts={"ACCOUNT": 10, "CUSTOMER": 5})
    target = _PlanTarget(existing_counts={"ACCOUNT": 0, "CUSTOMER": 0})
    plan = migrator.plan_schema(source, target, [account, customer])
    names = [t.table_name for t in plan.tables]
    assert names.index("CUSTOMER") < names.index("ACCOUNT")
    assert plan.total_source_rows == 15
    assert plan.not_ready == []


def test_plan_schema_not_ready_lists_only_unready_tables():
    account = _table("ACCOUNT")
    customer = _table("CUSTOMER")
    source = _PlanSource({}, counts={"ACCOUNT": 1, "CUSTOMER": 1})
    target = _PlanTarget(existing_counts={"CUSTOMER": 0}, missing={"ACCOUNT"})
    plan = migrator.plan_schema(source, target, [account, customer])
    assert plan.not_ready == ["ACCOUNT"]


# ---------------------------------------------------- dependency waves


def test_order_tables_by_dependency_waves_groups_independent_tables_together():
    account = _table("ACCOUNT", fk_to="CUSTOMER")
    customer = _table("CUSTOMER")
    supplier = _table("SUPPLIER")  # no FK at all -- independent of everything
    waves = migrator.order_tables_by_dependency_waves([account, customer, supplier])
    wave_names = [sorted(t.name for t in w) for w in waves]
    # CUSTOMER and SUPPLIER have no unmet dependency, so they land in the
    # same first wave; ACCOUNT can't start until CUSTOMER's wave is done.
    assert wave_names[0] == ["CUSTOMER", "SUPPLIER"]
    assert wave_names[1] == ["ACCOUNT"]


def test_order_tables_by_dependency_waves_multi_level_chain():
    transaction_history = _table("TRANSACTION_HISTORY", fk_to="ACCOUNT")
    account = _table("ACCOUNT", fk_to="CUSTOMER")
    customer = _table("CUSTOMER")
    waves = migrator.order_tables_by_dependency_waves([transaction_history, account, customer])
    assert [[t.name for t in w] for w in waves] == [["CUSTOMER"], ["ACCOUNT"], ["TRANSACTION_HISTORY"]]


def test_order_tables_by_dependency_waves_flattened_matches_flat_function():
    # order_tables_by_dependency is now defined in terms of the waves
    # function -- this pins down that the refactor produces byte-for-byte
    # the same flat order as before, not just an equivalent one.
    transaction_history = _table("TRANSACTION_HISTORY", fk_to="ACCOUNT")
    account = _table("ACCOUNT", fk_to="CUSTOMER")
    customer = _table("CUSTOMER")
    supplier = _table("SUPPLIER")
    tables = [transaction_history, account, customer, supplier]
    flat = migrator.order_tables_by_dependency(tables)
    flattened_waves = [t for wave in migrator.order_tables_by_dependency_waves(tables) for t in wave]
    assert [t.name for t in flat] == [t.name for t in flattened_waves]


def test_order_tables_by_dependency_waves_cycle_becomes_one_final_wave():
    a = _table("A", fk_to="B")
    b = _table("B", fk_to="A")
    waves = migrator.order_tables_by_dependency_waves([a, b])
    assert len(waves) == 1
    assert {t.name for t in waves[0]} == {"A", "B"}


# ---------------------------------------------------- LOB-aware batch sizing


def test_table_has_lob_columns_detects_clob_nclob_blob_long_variants():
    for lob_type in ["CLOB", "NCLOB", "BLOB", "LONG", "LONG RAW"]:
        t = Table(name="T", schema="HR", columns=[Column(name="C", data_type=lob_type)])
        assert migrator._table_has_lob_columns(t) is True, lob_type


def test_table_has_lob_columns_false_for_ordinary_and_sized_raw_types():
    t = Table(name="T", schema="HR", columns=[
        Column(name="ID", data_type="NUMBER"),
        Column(name="NAME", data_type="VARCHAR2(100)"),
        Column(name="TOK", data_type="RAW(2000)"),  # sized RAW is not a LOB
    ])
    assert migrator._table_has_lob_columns(t) is False


def test_effective_batch_size_shrinks_only_for_lob_tables():
    lob_table = Table(name="DOCS", schema="HR", columns=[Column(name="BODY", data_type="CLOB")])
    plain_table = Table(name="USERS", schema="HR", columns=[Column(name="NAME", data_type="VARCHAR2(100)")])
    assert migrator._effective_batch_size(lob_table, 5000, 200) == 200
    assert migrator._effective_batch_size(plain_table, 5000, 200) == 5000
    # Never *raises* the batch size even if lob_batch_size is larger.
    assert migrator._effective_batch_size(lob_table, 50, 200) == 50


def test_effective_batch_size_none_disables_narrowing():
    lob_table = Table(name="DOCS", schema="HR", columns=[Column(name="BODY", data_type="CLOB")])
    assert migrator._effective_batch_size(lob_table, 5000, None) == 5000


def test_migrate_table_requests_shrunk_batch_size_for_lob_table():
    docs = Table(name="DOCS", schema="HR", columns=[
        Column(name="ID", data_type="NUMBER"), Column(name="BODY", data_type="CLOB"),
    ])
    requested_sizes = []

    class _RecordingSource:
        def fetch_batches(self, sql, batch_size=5000):
            requested_sizes.append(batch_size)
            yield ["ID", "BODY"], [(1, "hello")]

    migrator.migrate_table(_RecordingSource(), _FakeTarget(), docs, batch_size=5000)
    assert requested_sizes == [2000]  # default lob_batch_size, not the full 5000


# --------------------------------------------------- LOB tables skip checksums


def test_migrate_table_skips_checksum_for_a_lob_table():
    # Explicit request after a real migration reported false "row count
    # matched but checksum did not" results on LOB-heavy tables: skip the
    # checksum (not just its comparison) entirely for a table with a LOB
    # column, so this can never happen again for one -- row-count
    # validation still runs and would still catch real data loss.
    docs = Table(name="DOCS", schema="HR", columns=[
        Column(name="ID", data_type="NUMBER"), Column(name="BODY", data_type="CLOB"),
    ])
    source = _FakeTableSource({"DOCS": (["ID", "BODY"], [(1, "hello"), (2, "world")])})
    target = _ValidatingTarget()
    result = migrator.migrate_table(source, target, docs)
    assert result.succeeded is True
    assert result.checksum is None
    assert result.validation.checksum_checked is False
    assert result.validation.row_counts_match is True  # unaffected -- still checked


def test_migrate_table_still_computes_checksum_for_a_non_lob_table():
    # Regression guard: the skip is specific to LOB tables, not a
    # blanket change to checksum behavior.
    users = _table("USERS")
    source = _FakeSource({"USERS": (["USERS_ID"], [(1,)])})
    target = _ValidatingTarget()
    result = migrator.migrate_table(source, target, users)
    assert result.succeeded is True
    assert result.checksum is not None
    assert result.validation.checksum_checked is True


# ---------------------------------------------------- parallel table migration


class _ThreadSafeConn:
    """A fake connector usable as both source and target, safe to use
    concurrently from multiple worker threads: fetch_batches/insert_batch
    each record which OS thread touched them (so a test can assert real
    concurrency happened, not just that the code path didn't crash)."""

    def __init__(self, rows_by_table=None, delay=0.0):
        self.rows_by_table = rows_by_table or {}
        self.delay = delay
        self._lock = threading.Lock()
        self.thread_ids = set()
        self.inserted_order = []
        self.closed = False

    def fetch_batches(self, sql, batch_size=5000):
        with self._lock:
            self.thread_ids.add(threading.get_ident())
        for name, (cols, rows) in self.rows_by_table.items():
            # Quoted now -- see tgdatabridge.db.source_sql.
            if f'."{name}"' in sql or f".{name}" in sql:
                if self.delay:
                    time.sleep(self.delay)
                yield cols, rows
                return
        yield [], []

    def insert_batch(self, table_name, columns, rows):
        with self._lock:
            self.thread_ids.add(threading.get_ident())
            self.inserted_order.append(table_name)

    def close(self):
        self.closed = True


def test_migrate_schema_max_workers_one_is_unchanged_serial_behavior():
    # Regression guard: the default (max_workers=1) path must produce
    # identical results to calling migrate_schema with no max_workers
    # argument at all -- every pre-existing test/caller in this file
    # relies on that.
    account = _table("ACCOUNT", fk_to="CUSTOMER")
    customer = _table("CUSTOMER")
    source = _FakeSource({
        "ACCOUNT": (["ACCOUNT_ID"], [(1,)]),
        "CUSTOMER": (["CUSTOMER_ID"], [(1,)]),
    })
    target = _FakeTarget()
    report = migrator.migrate_schema(source, target, [account, customer], max_workers=1)
    assert target.inserted_order == ["CUSTOMER", "ACCOUNT"]
    assert report.failed_tables == []


def test_migrate_schema_max_workers_without_factories_raises_value_error():
    account = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _FakeTarget()
    try:
        migrator.migrate_schema(source, target, [account], max_workers=4)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "source_factory" in str(exc) and "target_factory" in str(exc)


def test_migrate_schema_parallel_migrates_independent_tables_concurrently():
    a = _table("A")
    b = _table("B")
    c = _table("C")
    rows = {n: ([f"{n}_ID"], [(1,)]) for n in ["A", "B", "C"]}

    created_sources = []
    created_targets = []

    def source_factory():
        conn = _ThreadSafeConn(rows, delay=0.05)
        created_sources.append(conn)
        return conn

    def target_factory():
        conn = _ThreadSafeConn()
        created_targets.append(conn)
        return conn

    report = migrator.migrate_schema(
        None, None, [a, b, c], max_workers=3,
        source_factory=source_factory, target_factory=target_factory,
    )
    assert [r.table_name for r in report.results] == ["A", "B", "C"]  # deterministic, input order
    assert all(r.succeeded for r in report.results)
    assert len(created_sources) == 3 and len(created_targets) == 3
    # More than one worker thread actually touched the source connectors --
    # proof this genuinely ran in parallel, not just "didn't crash".
    all_source_threads = set()
    for conn in created_sources:
        all_source_threads |= conn.thread_ids
    assert len(all_source_threads) > 1
    assert all(conn.closed for conn in created_sources)
    assert all(conn.closed for conn in created_targets)


def test_migrate_schema_parallel_respects_fk_wave_ordering():
    account = _table("ACCOUNT", fk_to="CUSTOMER")
    customer = _table("CUSTOMER")
    rows = {n: ([f"{n}_ID"], [(1,)]) for n in ["ACCOUNT", "CUSTOMER"]}
    events = []
    lock = threading.Lock()

    class _TimedConn(_ThreadSafeConn):
        def insert_batch(self, table_name, columns, rows):
            super().insert_batch(table_name, columns, rows)
            with lock:
                events.append((table_name, time.time()))

    def source_factory():
        return _ThreadSafeConn(rows, delay=0.05)

    def target_factory():
        return _TimedConn()

    migrator.migrate_schema(
        None, None, [account, customer], max_workers=4,
        source_factory=source_factory, target_factory=target_factory,
    )
    customer_time = next(t for name, t in events if name == "CUSTOMER")
    account_time = next(t for name, t in events if name == "ACCOUNT")
    assert customer_time < account_time


def test_migrate_schema_parallel_uses_at_most_max_workers_connections():
    tables = [_table(f"T{i}") for i in range(5)]
    rows = {f"T{i}": ([f"T{i}_ID"], [(1,)]) for i in range(5)}
    created_sources = []

    def source_factory():
        conn = _ThreadSafeConn(rows, delay=0.02)
        created_sources.append(conn)
        return conn

    def target_factory():
        return _ThreadSafeConn()

    migrator.migrate_schema(
        None, None, tables, max_workers=2,
        source_factory=source_factory, target_factory=target_factory,
    )
    # 5 independent tables (one wave), but capped at max_workers=2 --
    # never more than 2 source connections ever created.
    assert len(created_sources) <= 2


def test_migrate_schema_parallel_calls_table_progress_cb_for_every_table():
    tables = [_table(f"T{i}") for i in range(4)]
    rows = {f"T{i}": ([f"T{i}_ID"], [(1,)]) for i in range(4)}
    calls = []
    lock = threading.Lock()

    def source_factory():
        return _ThreadSafeConn(rows)

    def target_factory():
        return _ThreadSafeConn()

    def table_progress(done, total):
        with lock:
            calls.append((done, total))

    migrator.migrate_schema(
        None, None, tables, max_workers=3,
        source_factory=source_factory, target_factory=target_factory,
        table_progress_cb=table_progress,
    )
    assert len(calls) == 4
    assert sorted(calls) == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_migrate_schema_parallel_skips_checkpointed_tables_without_connecting():
    from tgdatabridge.utils.app_storage import MigrationCheckpoint, TableCheckpoint

    account = _table("ACCOUNT")
    customer = _table("CUSTOMER")
    rows = {"ACCOUNT": (["ACCOUNT_ID"], [(1,)])}
    checkpoint = MigrationCheckpoint(
        checkpoint_id="cp", schema_name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    checkpoint.tables["CUSTOMER"] = TableCheckpoint(status="done", rows_copied=42, batches_completed=1)

    def source_factory():
        return _ThreadSafeConn(rows)

    def target_factory():
        return _ThreadSafeConn()

    report = migrator.migrate_schema(
        None, None, [account, customer], max_workers=2,
        source_factory=source_factory, target_factory=target_factory,
        checkpoint=checkpoint,
    )
    customer_result = next(r for r in report.results if r.table_name == "CUSTOMER")
    assert customer_result.skipped is True
    assert customer_result.rows_copied == 42


def test_migrate_schema_parallel_connection_factory_failure_becomes_failed_result():
    account = _table("ACCOUNT")

    def bad_source_factory():
        raise RuntimeError("could not connect")

    def target_factory():
        return _ThreadSafeConn()

    report = migrator.migrate_schema(
        None, None, [account], max_workers=2,
        source_factory=bad_source_factory, target_factory=target_factory,
    )
    assert len(report.results) == 1
    assert report.results[0].succeeded is False
    assert "could not connect" in report.results[0].error


# --------------------------------------------------------------- pause/resume


class _MultiBatchSource:
    """Yields several batches for one table, one at a time -- unlike
    _FakeSource above (which yields its whole result in a single batch),
    this is what a pause test actually needs: something to pause *between*.
    """

    def __init__(self, table_name, batches):
        self.table_name = table_name
        self.batches = batches
        self.batches_yielded = 0

    def fetch_batches(self, sql, batch_size=5000):
        for columns, rows in self.batches:
            yield columns, rows
            self.batches_yielded += 1


def test_migrate_table_pauses_between_batches_when_the_event_is_cleared():
    # The exact real-world request: a Pause button in the GUI should stop
    # a migration between batches, not abort it -- the source/target
    # connections stay open, and un-pausing continues from right where it
    # left off. Modeled here as a background thread doing the migration
    # while the main thread controls the event, the same relationship the
    # GUI (worker thread) and the Pause button (GUI thread) have.
    table = _table("ACCOUNT")
    source = _MultiBatchSource("ACCOUNT", [
        (["ACCOUNT_ID"], [(1,)]),
        (["ACCOUNT_ID"], [(2,)]),
        (["ACCOUNT_ID"], [(3,)]),
    ])
    target = _FakeTarget()
    pause_event = threading.Event()
    pause_event.clear()  # start paused, exactly like a Pause pressed before any batch runs

    result_holder = {}

    def run():
        result_holder["result"] = migrator.migrate_table(
            source, target, table, batch_size=1, pause_event=pause_event,
        )

    thread = threading.Thread(target=run)
    thread.start()
    try:
        # Give the worker thread every chance to (wrongly) proceed if the
        # pause check were missing or inverted -- it must not have written
        # anything to the target yet.
        time.sleep(0.2)
        assert target.inserted_order == []
        pause_event.set()  # resume
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        pause_event.set()  # never leave a thread blocked if an assertion above failed
    assert result_holder["result"].succeeded is True
    assert result_holder["result"].rows_copied == 3
    assert target.inserted_order == ["ACCOUNT", "ACCOUNT", "ACCOUNT"]


def test_migrate_table_with_no_pause_event_behaves_exactly_as_before():
    table = _table("ACCOUNT")
    source = _FakeSource({"ACCOUNT": (["ACCOUNT_ID"], [(1,)])})
    target = _FakeTarget()
    result = migrator.migrate_table(source, target, table, pause_event=None)
    assert result.succeeded is True
    assert result.rows_copied == 1


def test_migrate_schema_threads_the_pause_event_through_to_migrate_table():
    # A thin end-to-end check that migrate_schema's single-threaded path
    # actually forwards pause_event rather than silently dropping it.
    account = _table("ACCOUNT")
    source = _MultiBatchSource("ACCOUNT", [
        (["ACCOUNT_ID"], [(1,)]),
        (["ACCOUNT_ID"], [(2,)]),
    ])
    target = _FakeTarget()
    pause_event = threading.Event()
    pause_event.clear()

    result_holder = {}

    def run():
        result_holder["report"] = migrator.migrate_schema(
            source, target, [account], batch_size=1, pause_event=pause_event,
        )

    thread = threading.Thread(target=run)
    thread.start()
    try:
        time.sleep(0.2)
        assert target.inserted_order == []
        pause_event.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        pause_event.set()
    assert result_holder["report"].results[0].succeeded is True
    assert result_holder["report"].results[0].rows_copied == 2
