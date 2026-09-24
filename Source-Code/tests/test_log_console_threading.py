"""
The log console must never be written from a background thread.

This is the crash behind "the app closes by itself when I click Apply DDL".
`logger.log()` invokes its listeners synchronously on whatever thread
logged, and `_Worker.run`'s exception handler logs the whole traceback --
from the worker. The console's listener used to call `appendHtml()` right
there, i.e. a Qt widget mutated off the GUI thread. On Windows that is an
access violation, and the process dies with no traceback and no dialog:

    Windows fatal exception: access violation
      File "tgdatabridge\\gui\\log_console.py", line 22 in _on_log
      File "tgdatabridge\\utils\\logger.py", line 139 in log
      File "tgdatabridge\\gui\\main_window.py", line 99 in run

Because it sits in the failure path, it only fired once something else had
already gone wrong -- so a rejected DDL statement killed the application
instead of reporting itself.
"""
import threading

import pytest

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")

from PySide6.QtCore import QThread, QTimer                    # noqa: E402
from PySide6.QtWidgets import QApplication                    # noqa: E402

from tgdatabridge.gui.log_console import LogConsole                  # noqa: E402
from tgdatabridge.utils import logger                                # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def console(qt_app):
    c = LogConsole()
    yield c
    logger._listeners[:] = [cb for cb in logger._listeners if getattr(cb, "__self__", None) is not c]


def test_listener_does_not_touch_the_widget_on_the_logging_thread(console, qt_app):
    """The whole fix in one assertion: the callback that runs on the worker
    thread must not have modified the widget by the time it returns."""
    touched_from = []
    original = LogConsole._append

    def spy(self, level, line):
        touched_from.append(QThread.currentThread())
        original(self, level, line)

    LogConsole._append = spy
    try:
        gui_thread = QThread.currentThread()
        done = threading.Event()

        def worker():
            logger.error("boom from a worker thread")
            done.set()

        t = threading.Thread(target=worker)
        t.start()
        done.wait(5)
        t.join(5)

        # The queued connection has not been delivered yet -- crucially,
        # nothing touched the widget while the worker was inside logger.
        assert touched_from == [], "widget was written from the logging thread"

        qt_app.processEvents()
        assert touched_from, "queued log line was never delivered to the GUI thread"
        assert all(th == gui_thread for th in touched_from), \
            "widget was written from a thread other than the GUI thread"
    finally:
        LogConsole._append = original


def test_the_line_actually_reaches_the_console(console, qt_app):
    logger.info("hello from the gui thread")
    qt_app.processEvents()
    assert "hello from the gui thread" in console.toPlainText()


def test_worker_thread_line_reaches_the_console_after_the_event_loop_turns(console, qt_app):
    done = threading.Event()
    threading.Thread(target=lambda: (logger.warning("late line"), done.set())).start()
    done.wait(5)
    qt_app.processEvents()
    assert "late line" in console.toPlainText()


def test_a_raising_subscriber_cannot_break_the_operation_that_logged(qt_app):
    """A display concern must never propagate into a migration."""
    def broken(_level, _line):
        raise RuntimeError("subscriber exploded")

    seen = []
    logger.subscribe(broken)
    logger.subscribe(lambda lvl, line: seen.append(line))
    try:
        logger.info("still fine")          # must not raise
        assert seen and "still fine" in seen[0], "later subscribers must still run"
    finally:
        logger._listeners[:] = [cb for cb in logger._listeners
                                if cb is not broken and not isinstance(cb, type(lambda: 0)) or True]
        logger._listeners[:] = [cb for cb in logger._listeners if cb is not broken]
