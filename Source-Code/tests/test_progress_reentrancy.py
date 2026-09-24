"""MainWindow._on_progress must survive the progress dialog vanishing
mid-call.

The crash, from a real MySQL -> PostgreSQL run::

    File "tgdatabridge\\gui\\main_window.py", line 415, in _on_progress
    AttributeError: 'NoneType' object has no attribute 'setLabelText'

It fired four times in one session -- once per long-running operation --
and reached the user as the C2 crash dialog, so a migration that had in
fact completed looked like it had blown up.

The mechanism is a Qt trap rather than an ordinary null check being
missed. `QProgressDialog.setValue()` calls `QApplication.processEvents()`
internally whenever the dialog is modal, and `_run_async` makes this one
WindowModal. So the worker thread's queued `finished_ok` signal can be
delivered *inside* that setValue call, running `_on_async_done` ->
`_close_progress_dialog`, which sets `_progress_dialog` to None. Control
returns to the next line, which dereferences it.

That is why the existing `if self._progress_dialog is not None` guard
didn't help, and why the traceback points at `setLabelText` -- the line
*after* setValue -- rather than at the first use of the dialog. The
attribute really was set when the guard ran.

These tests need PySide6 and skip without it, in the same way the
PostgreSQL integration tests need a server. They call the real, unbound
`_on_progress` with a minimal stand-in for MainWindow rather than
building the whole window, so the code under test is genuinely the
shipped implementation.
"""
import pytest

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")

from PySide6.QtCore import Qt, QTimer                       # noqa: E402
from PySide6.QtWidgets import QApplication, QProgressDialog, QWidget  # noqa: E402

from tgdatabridge.gui.main_window import MainWindow                # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class _StubWindow:
    """Just enough of MainWindow for _on_progress to run: the status-bar
    progress bar it always touches, and the dialog attribute it guards."""

    class _Bar:
        def __init__(self):
            self.range = None
            self.value = None

        def setRange(self, low, high):
            self.range = (low, high)

        def setValue(self, value):
            self.value = value

    def __init__(self, dialog=None):
        self.progress = self._Bar()
        self._progress_dialog = dialog


def _modal_dialog(qt_app, parent):
    """A dialog in the same state _run_async leaves one in, and actually
    shown -- Qt only re-enters the event loop from setValue once the
    dialog has been displayed at least once."""
    dialog = QProgressDialog("Migrate Data…", None, 0, 0, parent)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setMinimumDuration(0)
    dialog.setCancelButton(None)
    dialog.show()
    qt_app.processEvents()
    dialog.setValue(1)
    qt_app.processEvents()
    return dialog


def test_survives_the_dialog_closing_inside_setvalue(qt_app):
    """The exact crash: a queued completion signal lands while setValue is
    processing events."""
    parent = QWidget()
    parent.show()
    window = _StubWindow(_modal_dialog(qt_app, parent))

    def completed():          # what _on_async_done -> _close_progress_dialog does
        window._progress_dialog.close()
        window._progress_dialog = None

    QTimer.singleShot(0, completed)

    MainWindow._on_progress(window, "Migrate Data", 3, 10)   # must not raise

    assert window._progress_dialog is None
    assert window.progress.value == 3        # the status bar still updated


def test_updates_the_dialog_normally_when_nothing_interrupts(qt_app):
    """The ordinary path has to keep working -- the fix must not have
    turned every update into a no-op."""
    parent = QWidget()
    parent.show()
    dialog = _modal_dialog(qt_app, parent)
    window = _StubWindow(dialog)

    MainWindow._on_progress(window, "Migrate Data", 4, 10)

    assert window._progress_dialog is dialog
    assert dialog.value() == 4
    assert dialog.maximum() == 10
    assert "4/10" in dialog.labelText()
    assert window.progress.range == (0, 10)


def test_no_dialog_at_all_is_fine(qt_app):
    """Progress can arrive after the dialog has already been closed."""
    window = _StubWindow(None)
    MainWindow._on_progress(window, "Migrate Data", 3, 10)
    assert window.progress.value == 3


def test_a_zero_total_is_ignored(qt_app):
    """An indeterminate task reports total=0; switching the bar to a
    0-of-0 range would make it look finished."""
    window = _StubWindow(None)
    MainWindow._on_progress(window, "Migrate Data", 0, 0)
    assert window.progress.range is None
    assert window.progress.value is None


def test_a_replaced_dialog_is_not_written_to(qt_app):
    """If the operation finished and a *new* one started inside setValue,
    the stale callback must not scribble on the new operation's dialog."""
    parent = QWidget()
    parent.show()
    first = _modal_dialog(qt_app, parent)
    second = _modal_dialog(qt_app, parent)
    second.setLabelText("Second operation…")
    window = _StubWindow(first)

    def swap():
        window._progress_dialog = second

    QTimer.singleShot(0, swap)
    MainWindow._on_progress(window, "Migrate Data", 3, 10)

    assert window._progress_dialog is second
    assert second.labelText() == "Second operation…"     # untouched
