"""Tests for app_storage.CheckpointWriter and the migrator's two-callback
checkpoint contract -- SCALE.md section 1.5.

migrate_schema used to persist the whole checkpoint file after *every*
batch. At the throughput the COPY path and intra-table sharding now reach
that's tens of full-file rewrites per second per shard, all serialized
behind one lock. CheckpointWriter coalesces those progress writes while
still writing terminal transitions (done/failed) immediately, because
those are exactly what a resume reads.

The clock and the save function are injected throughout, so nothing here
sleeps or touches a real disk except the round-trip test at the end.

Plain functions, no pytest -- see tests/test_cli_config.py's note.
"""
import pathlib
import tempfile
import threading

from tgdatabridge.core import migrator
from tgdatabridge.core.schema_model import Column, Table
from tgdatabridge.utils import app_storage


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _RecordingSave:
    def __init__(self, fail_times=0):
        self.calls = 0
        self.fail_times = fail_times

    def __call__(self, checkpoint, base_dir=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise OSError("disk full")


def _checkpoint():
    return app_storage.MigrationCheckpoint(
        checkpoint_id="cp", schema_name="HR",
        source_engine="Oracle", target_engine="PostgreSQL",
    )


def _writer(interval=5.0, fail_times=0):
    clock, save = _FakeClock(), _RecordingSave(fail_times=fail_times)
    writer = app_storage.CheckpointWriter(
        _checkpoint(), min_interval_seconds=interval, save_fn=save, time_fn=clock)
    return writer, clock, save


# --------------------------------------------------------------- throttling

def test_first_request_always_writes():
    # An early "in_progress" marker is what makes a crash in the first
    # seconds of a long run resumable at all.
    writer, _, save = _writer()
    assert writer.request() is True
    assert save.calls == 1


def test_requests_inside_the_interval_are_coalesced():
    writer, clock, save = _writer(interval=5.0)
    writer.request()
    for _ in range(100):
        clock.advance(0.01)
        assert writer.request() is False
    assert save.calls == 1
    assert writer.skipped == 100


def test_a_request_after_the_interval_writes_again():
    writer, clock, save = _writer(interval=5.0)
    writer.request()
    clock.advance(5.0)
    assert writer.request() is True
    assert save.calls == 2


def test_interval_boundary_is_inclusive():
    writer, clock, _ = _writer(interval=5.0)
    writer.request()
    clock.advance(4.999)
    assert writer.request() is False
    clock.advance(0.001)
    assert writer.request() is True


def test_zero_interval_writes_every_time():
    # Exactly the pre-throttling behavior, for anyone who wants the
    # narrowest possible re-work window on a crash.
    writer, clock, save = _writer(interval=0.0)
    for _ in range(20):
        clock.advance(0.001)
        assert writer.request() is True
    assert save.calls == 20


def test_negative_interval_is_clamped_to_zero():
    writer, _, save = _writer(interval=-3.0)
    assert writer.min_interval_seconds == 0.0
    writer.request()
    writer.request()
    assert save.calls == 2


# ------------------------------------------------------------ forced flushes

def test_flush_always_writes_even_inside_the_interval():
    writer, clock, save = _writer(interval=60.0)
    writer.request()
    clock.advance(0.001)
    assert writer.flush() is True
    assert save.calls == 2


def test_flush_resets_the_throttle_window():
    # A flush is a write; the next request shouldn't immediately write
    # again just because request() hasn't run recently.
    writer, clock, save = _writer(interval=5.0)
    writer.flush()
    clock.advance(1.0)
    assert writer.request() is False
    assert save.calls == 1


def test_repeated_flushes_all_write():
    writer, _, save = _writer(interval=60.0)
    for _ in range(5):
        writer.flush()
    assert save.calls == 5


# --------------------------------------------------------------- resilience

def test_a_failed_write_does_not_raise():
    # Persisting progress is a resilience feature; failing to write it
    # must never take down a migration that is otherwise succeeding.
    writer, _, _ = _writer(fail_times=1)
    assert writer.request() is False


def test_a_failed_write_is_retried_on_the_next_request():
    # The throttle window must not advance on a failed write, or a
    # transient disk problem would silently suppress the next interval's
    # write too.
    writer, clock, save = _writer(interval=5.0, fail_times=1)
    assert writer.request() is False   # attempt 1, failed
    clock.advance(0.001)
    assert writer.request() is True    # retried immediately, succeeded
    assert save.calls == 2


def test_writes_counter_only_counts_successes():
    writer, _, _ = _writer(fail_times=1)
    writer.request()
    writer.flush()
    assert writer.writes == 1


# ------------------------------------------------------------ thread safety

def test_concurrent_requests_still_respect_the_interval():
    # Sharded migrations call this from several worker threads at once.
    # Without a lock around the interval check, N threads could all decide
    # to write simultaneously and defeat the throttling entirely.
    writer, _, save = _writer(interval=60.0)
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        for _ in range(50):
            writer.request()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert save.calls == 1, f"expected coalescing to one write, got {save.calls}"


# ------------------------------------------------------- real disk round trip

def test_writer_persists_something_loadable():
    base = pathlib.Path(tempfile.mkdtemp(prefix="tgdatabridge_cpw_"))
    checkpoint = _checkpoint()
    checkpoint.tables["EMPLOYEES"] = app_storage.TableCheckpoint(status="done", rows_copied=42)
    writer = app_storage.CheckpointWriter(checkpoint, base_dir=base, min_interval_seconds=0.0)
    assert writer.flush() is True

    reloaded = app_storage.load_checkpoint("cp", base_dir=base)
    assert reloaded is not None
    assert reloaded.tables["EMPLOYEES"].rows_copied == 42


# ------------------------------------------- the migrator's two-callback split


def _table():
    table = Table(name="T", schema="HR")
    table.columns = [Column(name="ID", data_type="NUMBER(10)")]
    return table


class _Source:
    def __init__(self, batches):
        self.batches = batches

    def fetch_batches(self, sql, batch_size=5000):
        for rows in self.batches:
            yield ["ID"], rows


class _Target:
    def __init__(self, fail=False):
        self.fail = fail
        self.rows = []

    def insert_batch(self, table, columns, rows):
        if self.fail:
            raise RuntimeError("target refused the batch")
        self.rows.extend(rows)


def _run_migrate_table(batches, fail=False, checkpoint=None):
    updates, flushes = [], []
    result = migrator.migrate_table(
        _Source(batches), _Target(fail=fail), _table(), batch_size=1,
        checkpoint=checkpoint or app_storage.TableCheckpoint(),
        on_checkpoint_update=lambda: updates.append(1),
        on_checkpoint_flush=lambda: flushes.append(1),
        validate=False,
    )
    return result, updates, flushes


def test_progress_updates_fire_per_batch():
    _, updates, _ = _run_migrate_table([[(1,)], [(2,)], [(3,)]])
    assert len(updates) == 3


def test_a_completed_table_forces_exactly_one_flush():
    result, _, flushes = _run_migrate_table([[(1,)], [(2,)]])
    assert result.succeeded is True
    assert len(flushes) == 1


def test_a_failed_table_forces_a_flush():
    # "failed" is what tells a resume this table needs redoing -- losing
    # it to coalescing would make the resume skip real work.
    result, _, flushes = _run_migrate_table([[(1,)]], fail=True)
    assert result.succeeded is False
    assert len(flushes) == 1


def test_flush_defaults_to_update_for_callers_predating_the_split():
    # Every caller written before on_checkpoint_flush existed passes only
    # on_checkpoint_update, and must keep writing on terminal transitions
    # exactly as it always did.
    calls = []
    migrator.migrate_table(
        _Source([[(1,)], [(2,)]]), _Target(), _table(), batch_size=1,
        checkpoint=app_storage.TableCheckpoint(),
        on_checkpoint_update=lambda: calls.append(1),
        validate=False,
    )
    # 2 per-batch updates + 1 terminal transition, all through the one
    # callback that was given.
    assert len(calls) == 3


def test_no_callbacks_at_all_is_still_fine():
    result, _, _ = _run_migrate_table([[(1,)]], checkpoint=None)
    assert result.succeeded is True


def test_terminal_state_is_recorded_on_the_checkpoint_itself():
    checkpoint = app_storage.TableCheckpoint()
    result, _, _ = _run_migrate_table([[(1,)], [(2,)]], checkpoint=checkpoint)
    assert result.succeeded is True
    assert checkpoint.status == "done"
    assert checkpoint.rows_copied == 2


def test_throttling_does_not_lose_the_done_marker():
    # The whole point of the split: with a long interval, per-batch
    # updates are coalesced away entirely, but the table still ends up
    # marked done on disk.
    clock, save = _FakeClock(), _RecordingSave()
    checkpoint = _checkpoint()
    table_checkpoint = checkpoint.tables.setdefault("T", app_storage.TableCheckpoint())
    writer = app_storage.CheckpointWriter(
        checkpoint, min_interval_seconds=3600.0, save_fn=save, time_fn=clock)

    migrator.migrate_table(
        _Source([[(i,)] for i in range(20)]), _Target(), _table(), batch_size=1,
        checkpoint=table_checkpoint,
        on_checkpoint_update=writer.request,
        on_checkpoint_flush=writer.flush,
        validate=False,
    )

    assert table_checkpoint.status == "done"
    # One write from the very first request, one forced by the terminal
    # flush -- not the 21 the un-throttled path would have made.
    assert save.calls == 2, save.calls
