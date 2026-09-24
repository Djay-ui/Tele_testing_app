"""Tests for tgdatabridge.utils.metrics -- operation duration/outcome recording
and the Prometheus textfile export (migration duration, rows/sec
throughput, error rate per engine pair). Every call uses an explicit
`base_dir` so nothing here ever touches the real user's AppData/home
directory. Plain functions, no pytest fixtures."""
import pathlib
import shutil
import tempfile

from tgdatabridge.utils import metrics


def _tmp_dir():
    return pathlib.Path(tempfile.mkdtemp(prefix="tgdatabridge_test_"))


def test_load_operations_empty_when_nothing_recorded():
    base = _tmp_dir()
    try:
        assert metrics.load_operations(base_dir=base) == []
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_record_operation_appends_and_loads():
    base = _tmp_dir()
    try:
        metrics.record_operation("Load Schema", 1.5, True, actor="alice", base_dir=base)
        metrics.record_operation("Load Schema", 2.5, False, actor="bob", base_dir=base)

        ops = metrics.load_operations(base_dir=base)
        assert len(ops) == 2
        assert ops[0]["operation"] == "Load Schema"
        assert ops[0]["duration_seconds"] == 1.5
        assert ops[0]["success"] is True
        assert ops[0]["actor"] == "alice"
        assert ops[1]["success"] is False
        assert ops[1]["actor"] == "bob"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_record_operation_defaults_actor_to_unknown():
    base = _tmp_dir()
    try:
        metrics.record_operation("Apply DDL to Target", 0.5, True, base_dir=base)
        ops = metrics.load_operations(base_dir=base)
        assert ops[0]["actor"] == "unknown"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_record_operation_carries_extra_dimensions():
    base = _tmp_dir()
    try:
        metrics.record_operation(
            "Migrate Data", 10.0, True, actor="carol", base_dir=base,
            source_engine="Oracle", target_engine="PostgreSQL", rows=1000, failed_tables=0,
        )
        ops = metrics.load_operations(base_dir=base)
        assert ops[0]["source_engine"] == "Oracle"
        assert ops[0]["target_engine"] == "PostgreSQL"
        assert ops[0]["rows"] == 1000
        assert ops[0]["failed_tables"] == 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_record_operation_caps_history_length():
    base = _tmp_dir()
    original_cap = metrics._MAX_RECORDS
    try:
        metrics._MAX_RECORDS = 3
        for i in range(5):
            metrics.record_operation("Load Schema", 1.0, True, base_dir=base)
        ops = metrics.load_operations(base_dir=base)
        assert len(ops) == 3
    finally:
        metrics._MAX_RECORDS = original_cap
        shutil.rmtree(base, ignore_errors=True)


def test_load_operations_survives_corrupted_trailing_line():
    base = _tmp_dir()
    try:
        metrics.record_operation("Load Schema", 1.0, True, base_dir=base)
        path = metrics._operations_path(base_dir=base)
        with path.open("a", encoding="utf-8") as f:
            f.write("{not valid json\n")
        ops = metrics.load_operations(base_dir=base)
        assert len(ops) == 1  # the corrupted trailing line is skipped, not fatal
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_export_prometheus_textfile_operation_duration_and_counts():
    base = _tmp_dir()
    try:
        metrics.record_operation("Load Schema", 1.0, True, base_dir=base)
        metrics.record_operation("Load Schema", 2.0, True, base_dir=base)
        metrics.record_operation("Load Schema", 3.0, False, base_dir=base)

        path = metrics.export_prometheus_textfile(base_dir=base)
        text = path.read_text(encoding="utf-8")

        assert 'tgdatabridge_operation_duration_seconds{operation="Load Schema"} 3.0' in text
        assert 'tgdatabridge_operation_runs_total{operation="Load Schema",outcome="success"} 2' in text
        assert 'tgdatabridge_operation_runs_total{operation="Load Schema",outcome="failure"} 1' in text
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_export_prometheus_textfile_migration_rows_per_second_and_error_rate():
    base = _tmp_dir()
    try:
        metrics.record_operation(
            "Migrate Data", 10.0, True, base_dir=base,
            source_engine="Oracle", target_engine="PostgreSQL", rows=1000, failed_tables=0,
        )
        metrics.record_operation(
            "Migrate Data", 5.0, True, base_dir=base,
            source_engine="Oracle", target_engine="PostgreSQL", rows=200, failed_tables=1,
        )

        text = metrics.prometheus_textfile_path(base_dir=base).read_text(encoding="utf-8")

        # Rows/sec is based on the *most recent* Migrate Data run for this
        # pair: 200 rows / 5.0s = 40.0.
        assert (
            'tgdatabridge_migration_rows_per_second{source_engine="Oracle",'
            'target_engine="PostgreSQL"} 40.0'
        ) in text
        # One of the two runs had a failed table -> error rate 1/2 = 0.5.
        assert (
            'tgdatabridge_migration_error_rate{source_engine="Oracle",'
            'target_engine="PostgreSQL"} 0.5'
        ) in text
        assert "tgdatabridge_migration_rows_total 1200" in text
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_export_prometheus_textfile_handles_no_data():
    base = _tmp_dir()
    try:
        path = metrics.export_prometheus_textfile(base_dir=base)
        text = path.read_text(encoding="utf-8")
        assert "tgdatabridge_migration_rows_total 0" in text
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_record_operation_is_best_effort_on_disk_failure():
    # Simulates an unwritable metrics directory -- record_operation must
    # never raise even if it can't persist anything.
    base = _tmp_dir()
    original_mkdir = pathlib.Path.mkdir
    try:
        def _boom(*args, **kwargs):
            raise OSError("disk full")

        pathlib.Path.mkdir = _boom
        metrics.record_operation("Load Schema", 1.0, True, base_dir=base)  # must not raise
    finally:
        pathlib.Path.mkdir = original_mkdir
        shutil.rmtree(base, ignore_errors=True)


def test_label_values_with_special_characters_are_escaped():
    base = _tmp_dir()
    try:
        metrics.record_operation(
            "Migrate Data", 1.0, True, base_dir=base,
            source_engine='Weird"Engine', target_engine="Other\\Engine", rows=1, failed_tables=0,
        )
        text = metrics.prometheus_textfile_path(base_dir=base).read_text(encoding="utf-8")
        assert 'source_engine="Weird\\"Engine"' in text
        assert 'target_engine="Other\\\\Engine"' in text
    finally:
        shutil.rmtree(base, ignore_errors=True)
