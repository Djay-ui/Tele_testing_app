"""Tests for tgdatabridge.utils.logger -- the structured, actor-stamped,
disk-persisted log bus. Every disk-touching test points logger at a
throwaway temp directory via logger.configure(base_dir=...) and always
restores it via logger.reset_for_tests() in a finally block, so nothing
here ever touches the real user's AppData/home directory or leaks state
into another test. Plain functions, no pytest fixtures -- matches
test_app_storage.py's fixture-free style so this runs the same way under
`pytest` or this repo's hand-rolled test runner."""
import json
import pathlib
import shutil
import tempfile

from tgdatabridge.utils import logger


def _tmp_dir():
    return pathlib.Path(tempfile.mkdtemp(prefix="tgdatabridge_test_"))


def _isolated(base_dir):
    """Points the module-global logger at `base_dir` and clears any
    listeners/history left over from a previous test."""
    logger.reset_for_tests()
    logger.configure(base_dir=base_dir)


def test_info_warning_error_reach_subscribers_with_formatted_line():
    base = _tmp_dir()
    try:
        _isolated(base)
        seen = []
        logger.subscribe(lambda level, line: seen.append((level, line)))

        logger.info("hello")
        logger.warning("careful")
        logger.error("boom")

        assert [level for level, _ in seen] == ["info", "warning", "error"]
        assert seen[0][1].endswith("hello")
        assert seen[0][1].startswith("[")  # "[HH:MM:SS] hello"
    finally:
        logger.reset_for_tests()
        shutil.rmtree(base, ignore_errors=True)


def test_history_is_structured_with_actor_and_timestamp():
    base = _tmp_dir()
    try:
        _isolated(base)
        logger.info("first")
        logger.warning("second")

        hist = logger.history()
        assert len(hist) == 2
        for record in hist:
            assert set(record.keys()) == {"timestamp", "level", "actor", "message"}
            assert record["actor"]  # never blank -- falls back to "unknown"
        assert hist[0]["message"] == "first"
        assert hist[0]["level"] == "info"
        assert hist[1]["message"] == "second"
        assert hist[1]["level"] == "warning"
    finally:
        logger.reset_for_tests()
        shutil.rmtree(base, ignore_errors=True)


def test_log_persists_json_lines_to_disk():
    base = _tmp_dir()
    try:
        _isolated(base)
        logger.info("first line")
        logger.error("second line")

        log_files = list((base / "logs").glob("tgdatabridge-*.jsonl"))
        assert len(log_files) == 1

        lines = log_files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["message"] == "first line"
        assert first["level"] == "info"
        assert "timestamp" in first and "actor" in first
        second = json.loads(lines[1])
        assert second["message"] == "second line"
        assert second["level"] == "error"
    finally:
        logger.reset_for_tests()
        shutil.rmtree(base, ignore_errors=True)


def test_disk_disabled_writes_nothing():
    base = _tmp_dir()
    try:
        logger.reset_for_tests()
        logger.configure(base_dir=base, disk_enabled=False)
        logger.info("should not be written")

        assert not (base / "logs").exists()
        # ...but in-memory history/subscribers still work normally.
        assert logger.history()[0]["message"] == "should not be written"
    finally:
        logger.reset_for_tests()
        shutil.rmtree(base, ignore_errors=True)


def test_disk_write_failure_never_raises():
    # No pytest fixtures here (this repo's test runner is fixture-free) --
    # patch Path.mkdir by hand and always restore it, even on failure.
    base = _tmp_dir()
    original_mkdir = pathlib.Path.mkdir
    try:
        _isolated(base)

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        pathlib.Path.mkdir = _boom
        # Should not raise even though the log directory can't be created.
        logger.info("still works")
        assert logger.history()[-1]["message"] == "still works"
    finally:
        pathlib.Path.mkdir = original_mkdir
        logger.reset_for_tests()
        shutil.rmtree(base, ignore_errors=True)


def test_subscribe_and_unsubscribe():
    base = _tmp_dir()
    try:
        _isolated(base)
        seen = []

        def cb(level, line):
            seen.append(line)

        logger.subscribe(cb)
        logger.info("one")
        logger.unsubscribe(cb)
        logger.info("two")

        assert len(seen) == 1
        assert seen[0].endswith("one")
    finally:
        logger.reset_for_tests()
        shutil.rmtree(base, ignore_errors=True)


def test_unsubscribe_unknown_callback_is_a_no_op():
    base = _tmp_dir()
    try:
        _isolated(base)
        logger.unsubscribe(lambda level, line: None)  # never subscribed -- must not raise
    finally:
        logger.reset_for_tests()
        shutil.rmtree(base, ignore_errors=True)


def test_current_actor_never_raises_and_returns_nonempty_string():
    actor = logger.current_actor()
    assert isinstance(actor, str)
    assert actor  # falls back to "unknown" rather than "" or None


def test_log_files_are_appended_across_multiple_calls_same_day():
    base = _tmp_dir()
    try:
        _isolated(base)
        for i in range(5):
            logger.info(f"line {i}")

        log_files = list((base / "logs").glob("tgdatabridge-*.jsonl"))
        assert len(log_files) == 1
        lines = log_files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 5
    finally:
        logger.reset_for_tests()
        shutil.rmtree(base, ignore_errors=True)


def test_logs_stay_local_even_when_shared_storage_is_configured(tmp_path, monkeypatch):
    """Logs are written to the machine that produced them, never to a
    configured team share.

    Profiles, history, checkpoints and metrics all follow shared storage --
    a team genuinely benefits from sharing those. A diagnostic record is
    different: pointing several analysts' logs at one folder makes it hard
    to tell whose run failed, and a briefly-unreachable network share
    turns into missing evidence for the run you most need to explain.
    """
    from tgdatabridge.utils import app_storage

    local = tmp_path / "local"
    shared = tmp_path / "shared"
    local.mkdir()
    shared.mkdir()
    # Configure a shared folder exactly as the Settings dialog would.
    (local / "settings.json").write_text(
        json.dumps({"shared_storage_path": str(shared)}), encoding="utf-8")
    monkeypatch.setattr(app_storage, "local_app_data_dir",
                        lambda base_dir=None: base_dir if base_dir is not None else local)

    # Everything else redirects to the share...
    assert app_storage.app_data_dir() == shared
    # ...but the log directory does not.
    assert logger.log_dir() == local / "logs"
