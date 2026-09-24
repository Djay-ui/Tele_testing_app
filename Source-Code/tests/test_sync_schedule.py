"""The scheduled sync control.

What matters here is not the widgets but the two behaviours that decide
whether a sync is safe to leave running unattended: a tick that arrives
while the previous run is still going must be dropped rather than queued,
and a run that fails must not leave the tool believing a sync is still in
progress -- which would silently stop every later tick.
"""
import pytest

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")

from PySide6.QtWidgets import QApplication, QMessageBox   # noqa: E402

from tgdatabridge.gui import main_window as MW                   # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    return MW.MainWindow()


def test_the_controls_exist_with_safe_defaults(window):
    assert window.sync_deletes_checkbox.isChecked() is True
    assert window.sync_interval_spin.value() == 15
    assert window.sync_schedule_btn.text() == "Start scheduled sync"
    assert window._sync_timer.isActive() is False


def test_the_interval_cannot_be_set_to_zero(window):
    """A zero-minute timer fires continuously and would hammer both
    databases."""
    window.sync_interval_spin.setValue(0)
    assert window.sync_interval_spin.value() >= 1


def test_a_sync_without_a_connection_explains_rather_than_crashing(window):
    window.source_params = None
    window._sync_changes(scheduled=False)          # must not raise
    assert window._sync_running is False


def test_a_scheduled_tick_without_a_connection_is_silent(window, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.append(a)))
    window.source_params = None
    window._sync_changes(scheduled=True)
    assert shown == []


def test_a_tick_arriving_mid_run_is_dropped_not_queued(window, monkeypatch):
    """Two syncs over the same tables at once race on the same rows and
    double-count what they moved."""
    started = []
    monkeypatch.setattr(MW.MainWindow, "_run_async",
                        lambda self, *a, **k: started.append(a[0]))
    from tgdatabridge.db.base import ConnectionParams
    from tgdatabridge.core.schema_model import Schema

    window.source_params = ConnectionParams("h", 1, "d", "u", "")
    window.target_params = ConnectionParams("h", 1, "d", "u", "")
    window.schema = Schema(name="app")

    window._sync_changes(scheduled=True)
    assert started == ["Sync Changes"]
    assert window._sync_running is True

    window._sync_changes(scheduled=True)           # the tick that lands busy
    assert started == ["Sync Changes"]             # still one


def test_a_failed_run_clears_the_in_progress_flag(window, monkeypatch):
    """Left set, it would silently stop every later tick."""
    captured = {}

    def fake_run_async(self, label, fn, on_success, *a, **kw):
        captured["on_failed"] = kw.get("on_failed")

    monkeypatch.setattr(MW.MainWindow, "_run_async", fake_run_async)
    from tgdatabridge.db.base import ConnectionParams
    from tgdatabridge.core.schema_model import Schema

    window.source_params = ConnectionParams("h", 1, "d", "u", "")
    window.target_params = ConnectionParams("h", 1, "d", "u", "")
    window.schema = Schema(name="app")
    window._sync_changes(scheduled=True)
    assert window._sync_running is True

    assert captured["on_failed"] is not None
    captured["on_failed"]("boom")
    assert window._sync_running is False


def test_starting_and_stopping_the_schedule(window, monkeypatch):
    monkeypatch.setattr(MW.MainWindow, "_sync_changes", lambda self, scheduled=False: None)
    window.sync_interval_spin.setValue(3)
    window._toggle_sync_schedule()
    assert window._sync_timer.isActive() is True
    assert window._sync_timer.interval() == 3 * 60 * 1000
    assert window.sync_schedule_btn.text() == "Stop scheduled sync"

    window._toggle_sync_schedule()
    assert window._sync_timer.isActive() is False
    assert window.sync_schedule_btn.text() == "Start scheduled sync"


def test_the_marks_are_stored_per_source_target_schema(window, tmp_path):
    """Two different migrations must not share one set of high-water
    marks, or each would skip the other's changes."""
    from tgdatabridge.db.base import ConnectionParams
    from tgdatabridge.core.schema_model import Schema

    window.schema = Schema(name="app")
    window.source_params = ConnectionParams("h", 1, "src_a", "u", "")
    window.target_params = ConnectionParams("h", 1, "dst", "u", "")
    first = window._sync_id()
    window.source_params = ConnectionParams("h", 1, "src_b", "u", "")
    assert window._sync_id() != first


def test_watermarks_survive_a_restart(tmp_path):
    from tgdatabridge.utils import app_storage

    app_storage.save_watermarks("abc", {"orders": "2026-06-01 12:00:00"}, base_dir=tmp_path)
    assert app_storage.load_watermarks("abc", base_dir=tmp_path) == {
        "orders": "2026-06-01 12:00:00"}


def test_an_unreadable_marks_file_means_read_everything_not_skip_everything(tmp_path):
    """Inventing a mark would silently skip rows; an empty one only costs
    a slower run."""
    from tgdatabridge.utils import app_storage

    path = app_storage._watermarks_path("abc", tmp_path)
    path.write_text("{ not json", encoding="utf-8")
    assert app_storage.load_watermarks("abc", base_dir=tmp_path) == {}
    assert app_storage.load_watermarks("never-saved", base_dir=tmp_path) == {}
