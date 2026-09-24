"""Tests for tgdatabridge.utils.crash -- ENTERPRISE_READINESS.md, C2.

The defect these cover: the packaged build sets console=False, nothing
installed a sys.excepthook, and nothing enabled faulthandler, so an
unhandled exception in the GUI wrote its traceback to a stream that does
not exist. No dialog, no log line, no file -- an unactionable bug report.

Everything here exercises the Qt-free half. The dialog itself
(tgdatabridge/gui/crash_dialog.py) needs a QApplication and is not covered by
this suite, which by design runs with no GUI dependencies installed at
all -- the same reason no other tests/ module imports PySide6. The split
between the two modules is what keeps the logic below testable.
"""
import sys
import threading

import pytest

from tgdatabridge.utils import crash, logger


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    """Point both the crash file and the ordinary log at a temp dir, and
    guarantee the real sys.excepthook is restored even if a test fails --
    a leaked hook would silently corrupt every later test in the run."""
    logger.reset_for_tests()
    logger.configure(base_dir=tmp_path)
    crash.uninstall()
    yield tmp_path
    crash.uninstall()
    logger.reset_for_tests()


def _boom(message="kaboom"):
    """Raise and return a real (exc_type, exc, tb) triple -- a genuine
    traceback object, not a fabricated one, so format_exception is
    exercised the way it will be in production."""
    try:
        raise ValueError(message)
    except ValueError:
        return sys.exc_info()


# ------------------------------------------------------------- install

def test_install_replaces_the_excepthook_and_uninstall_restores_it(tmp_path):
    original = sys.excepthook
    crash.install(base_dir=tmp_path)
    assert sys.excepthook is not original
    crash.uninstall()
    assert sys.excepthook is original


def test_install_is_idempotent(tmp_path):
    """Installing twice must not chain the handler onto itself -- that
    would report (and dialog) every crash twice."""
    original = sys.excepthook
    crash.install(base_dir=tmp_path)
    first = sys.excepthook
    crash.install(base_dir=tmp_path)
    assert sys.excepthook is first
    crash.uninstall()
    assert sys.excepthook is original


def test_install_survives_an_unwritable_crash_directory(tmp_path, monkeypatch):
    """A locked-down AppData folder must not stop the application
    starting -- the Python-level hooks still work even when faulthandler
    cannot open its file."""
    monkeypatch.setattr(crash, "crash_log_path",
                        lambda base_dir=None: tmp_path / "nope" / "\0bad" / "c.log")
    fault_ok = crash.install(base_dir=tmp_path)
    assert fault_ok is False          # faulthandler could not be enabled...
    assert sys.excepthook is not sys.__excepthook__   # ...but the hook is live


# ------------------------------------------------------------ capturing

def test_unhandled_exception_is_written_to_the_crash_file(tmp_path):
    crash.install(base_dir=tmp_path)
    crash.handle_exception(*_boom("disk on fire"))

    path = crash.last_crash_path()
    assert path is not None and path.exists()
    text = path.read_text(encoding="utf-8")
    assert "ValueError: disk on fire" in text
    assert "Traceback (most recent call last)" in text
    assert "_boom" in text          # the real stack, not just the message


def test_unhandled_exception_reaches_the_log_bus(tmp_path):
    """So the crash also appears in the GUI console, the day's JSONL file
    and any configured central log sink."""
    crash.install(base_dir=tmp_path)
    crash.handle_exception(*_boom("into the log"))

    messages = [r["message"] for r in logger.history()]
    assert any("Unhandled ValueError" in m and "into the log" in m for m in messages)
    assert any("Traceback (most recent call last)" in m for m in messages)
    assert all(r["level"] == "error" for r in logger.history())


def test_notify_callback_receives_summary_and_path(tmp_path):
    seen = []
    crash.install(base_dir=tmp_path, notify=lambda s, p: seen.append((s, p)))
    crash.handle_exception(*_boom("tell the user"))

    assert len(seen) == 1
    summary, path = seen[0]
    assert "Unhandled ValueError: tell the user" == summary
    assert path is not None and path.exists()


def test_two_crashes_both_land_in_the_same_file(tmp_path):
    crash.install(base_dir=tmp_path)
    crash.handle_exception(*_boom("first"))
    crash.handle_exception(*_boom("second"))
    text = crash.last_crash_path().read_text(encoding="utf-8")
    assert "first" in text and "second" in text


# ----------------------------------------------------------- robustness

def test_keyboard_interrupt_is_not_treated_as_a_crash(tmp_path):
    """Ctrl-C is a user action. Reporting it as a crash would be noise,
    and swallowing it would break the CLI's interrupt behaviour."""
    seen = []
    crash.install(base_dir=tmp_path, notify=lambda s, p: seen.append(s))
    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        crash.handle_exception(*sys.exc_info())

    assert seen == []
    assert crash.last_crash_path() is None
    assert not any("Unhandled" in r["message"] for r in logger.history())


def test_a_failing_notify_callback_cannot_break_the_handler(tmp_path):
    """The dialog runs while the process is already in an unknown state.
    If it throws, the crash must still be recorded."""
    def bad_notify(summary, path):
        raise RuntimeError("the dialog itself broke")

    crash.install(base_dir=tmp_path, notify=bad_notify)
    crash.handle_exception(*_boom("still recorded"))       # must not raise

    assert "still recorded" in crash.last_crash_path().read_text(encoding="utf-8")


def test_a_failing_logger_cannot_break_the_handler(tmp_path, monkeypatch):
    """If logging is the thing that broke, the crash file must still be
    written -- which is why it does not go through logger.py's own path."""
    def bad_error(message):
        raise OSError("log device gone")

    crash.install(base_dir=tmp_path)
    monkeypatch.setattr(logger, "error", bad_error)
    crash.handle_exception(*_boom("logger is down"))       # must not raise

    assert "logger is down" in crash.last_crash_path().read_text(encoding="utf-8")


def test_an_unwritable_crash_file_still_notifies_with_a_none_path(tmp_path, monkeypatch):
    """The dialog distinguishes 'details are in this file' from 'details
    could not be saved', so the user is never sent to a path that does
    not exist."""
    seen = []
    crash.install(base_dir=tmp_path, notify=lambda s, p: seen.append((s, p)))
    monkeypatch.setattr(crash, "_write_crash_file", lambda text: None)
    crash.handle_exception(*_boom("nowhere to write"))

    assert len(seen) == 1
    assert seen[0][1] is None


def test_handler_does_not_recurse_when_the_crash_report_itself_crashes(tmp_path):
    """A crash inside the crash handler must not re-enter it. Without the
    guard this is an infinite loop that takes the process down harder
    than the original fault did."""
    depth = {"n": 0, "max": 0}

    def reentrant_notify(summary, path):
        depth["n"] += 1
        depth["max"] = max(depth["max"], depth["n"])
        try:
            if depth["n"] < 5:
                crash.handle_exception(*_boom("nested"))
        finally:
            depth["n"] -= 1

    crash.install(base_dir=tmp_path, notify=reentrant_notify)
    crash.handle_exception(*_boom("outer"))
    assert depth["max"] == 1


# -------------------------------------------------------------- threads

def test_worker_thread_exception_is_captured(tmp_path):
    """A bare threading.Thread started outside Qt
    does not route through the GUI worker's try/except, so it needs
    threading.excepthook to be caught at all."""
    crash.install(base_dir=tmp_path)

    def explode():
        raise RuntimeError("thread died")

    t = threading.Thread(target=explode, name="shipper-1")
    t.start()
    t.join()

    path = crash.last_crash_path()
    assert path is not None
    assert "RuntimeError: thread died" in path.read_text(encoding="utf-8")
    assert any("in thread shipper-1" in r["message"] for r in logger.history())


# ------------------------------------------------- caught-but-unexpected

def test_record_handled_exception_writes_to_the_same_file(tmp_path):
    """The GUI worker catches everything so it can show the user a
    message rather than dying. That is right, but it used to mean an
    unexpected error left support with nothing -- this puts the stack in
    the crash file without changing what the user sees."""
    crash.install(base_dir=tmp_path)
    detail = crash.format_exception(*_boom("caught in a worker"))
    path = crash.record_handled_exception(detail)

    assert path is not None and path.exists()
    assert "caught in a worker" in path.read_text(encoding="utf-8")


def test_format_exception_returns_a_real_traceback():
    text = crash.format_exception(*_boom("formatted"))
    assert "Traceback (most recent call last)" in text
    assert "ValueError: formatted" in text
    assert "_boom" in text


def test_format_exception_never_raises_on_a_malformed_triple():
    # Defensive: this runs while the process is already unhealthy, so it
    # must tolerate being handed something odd rather than adding a second
    # exception on top of the first.
    text = crash.format_exception(ValueError, ValueError("x"), "not-a-traceback")
    assert "ValueError" in text
