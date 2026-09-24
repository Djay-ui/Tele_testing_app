"""Bottom log console panel."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit

from tgdatabridge.utils import logger

_COLORS = {"info": "#1a1a1a", "warning": "#ef6c00", "error": "#c62828"}


class LogConsole(QPlainTextEdit):
    """Live view of the log bus.

    The subtlety here is threading, and getting it wrong killed the
    application outright. `logger.log()` calls its listeners synchronously,
    on whatever thread produced the message -- and plenty of messages come
    from the background worker: every `logger.error(...)` in `_Worker.run`'s
    exception handler, and the per-table progress lines during a migration.

    Qt widgets may only be touched from the GUI thread. Calling
    `appendHtml()` straight from a worker is undefined behaviour, and on
    Windows it reliably surfaced as::

        Windows fatal exception: access violation
          File "tgdatabridge\\gui\\log_console.py", line 22 in _on_log
          File "tgdatabridge\\utils\\logger.py", line 139 in log
          File "tgdatabridge\\gui\\main_window.py", line 99 in run

    -- the process dying mid-operation with no Python traceback and no
    error dialog. Because the offending call sits in the *failure* path, it
    only ever showed up when something else had already gone wrong: a
    rejected DDL statement would kill the app instead of reporting itself,
    which is precisely the "it just closes when I click Apply DDL" symptom.

    The fix is the standard Qt one: the listener callback does nothing but
    emit a signal. Qt sees that sender and receiver live on different
    threads and queues the call onto the GUI thread's event loop, so the
    widget is only ever written from the thread that owns it.
    """

    #: Carries (level, line) from whatever thread logged to the GUI thread.
    log_received = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.setStyleSheet("font-family: Consolas, monospace; font-size: 12px; background:#111; color:#ddd;")
        # AutoConnection already resolves to a queued connection for a
        # cross-thread emit; naming it explicitly documents the intent and
        # keeps it correct even if this ever runs on the GUI thread only.
        self.log_received.connect(self._append, Qt.ConnectionType.QueuedConnection)
        logger.subscribe(self._on_log)

    def _on_log(self, level: str, line: str) -> None:
        """Runs on the *logging* thread -- must not touch the widget."""
        try:
            self.log_received.emit(level, line)
        except RuntimeError:
            # The widget was destroyed during shutdown while a worker was
            # still logging. Losing a console line at that point is
            # immaterial; raising here would propagate into whatever
            # operation produced the message.
            pass

    def _append(self, level: str, line: str) -> None:
        """Runs on the GUI thread, via the queued connection above."""
        color = _COLORS.get(level, "#ddd")
        self.appendHtml(f'<span style="color:{color}">{line}</span>')
        self.moveCursor(QTextCursor.MoveOperation.End)
