"""The connection dialog has to fit on the screen it opens on.

Adding "How to reach it" made it tall enough that on a 1080p laptop with
a taskbar the OK / Cancel / Test Connection row fell below the bottom of
the screen -- and a dialog whose only way out is Alt+F4 is worse than no
dialog at all.
"""
import pytest

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")

from PySide6.QtGui import QGuiApplication          # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402

from tgdatabridge.gui.connection_dialog import ConnectionDialog  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def _available():
    return QGuiApplication.primaryScreen().availableGeometry()


@pytest.mark.parametrize("engine", ["MySQL", "PostgreSQL", "Oracle", "SQL Server",
                                    "DB2", "MongoDB", "Excel/CSV"])
def test_every_engine_opens_inside_the_usable_screen_area(qt_app, engine):
    dialog = ConnectionDialog(engine, role="source")
    dialog.show()
    available = _available()
    assert dialog.height() <= available.height()
    assert dialog.width() <= available.width()
    assert dialog.geometry().top() >= available.top()
    dialog.close()


def test_the_buttons_are_never_inside_the_scrolling_part(qt_app):
    """They are what the user needs when the form is too tall, so they
    must not be the thing that scrolls away."""
    dialog = ConnectionDialog("MySQL")
    scroll = dialog.findChild(QScrollArea)
    assert scroll is not None
    assert dialog.test_button.parentWidget() is dialog
    assert dialog._button_box.parentWidget() is dialog
    assert not scroll.isAncestorOf(dialog.test_button)
    assert not scroll.isAncestorOf(dialog._button_box)


def test_choosing_the_jump_host_makes_room_for_its_fields(qt_app):
    dialog = ConnectionDialog("MySQL")
    dialog.show()
    before = dialog.height()
    dialog.access_ssh_radio.setChecked(True)
    assert dialog.height() > before, "the revealed fields should be given room"
    assert dialog.height() <= _available().height()
    dialog.close()


def test_it_never_grows_past_the_screen_even_with_everything_shown(qt_app, monkeypatch):
    """The cap is what matters on a small laptop: past it the form
    scrolls instead of running off the bottom."""
    available = _available()
    tiny = available.adjusted(0, 0, 0, -(available.height() - 300))
    monkeypatch.setattr(
        type(QGuiApplication.primaryScreen()), "availableGeometry",
        lambda self: tiny)
    dialog = ConnectionDialog("PostgreSQL")
    dialog.access_ssh_radio.setChecked(True)
    dialog.show()
    assert dialog.height() <= tiny.height()
    scroll = dialog.findChild(QScrollArea)
    # the content genuinely does not fit -- which is exactly when the
    # scroll area has to be doing its job
    assert scroll.widget().sizeHint().height() > scroll.height()
    dialog.close()


def test_the_selected_mode_is_readable_without_relying_on_the_indicator(qt_app):
    """Some Windows themes draw a selected radio as a small, low-contrast
    dot. This choice decides whether the connection can work at all."""
    dialog = ConnectionDialog("MySQL")
    assert dialog.access_direct_radio.font().bold()
    assert not dialog.access_ssh_radio.font().bold()
    dialog.access_ssh_radio.setChecked(True)
    assert dialog.access_ssh_radio.font().bold()
    assert not dialog.access_direct_radio.font().bold()
