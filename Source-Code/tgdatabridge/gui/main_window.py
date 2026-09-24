"""Main application window: connection toolbar, schema tree, DDL/report tabs,
and a log console — the same overall layout AWS SCT itself uses."""
from __future__ import annotations

import datetime
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QProgressBar, QProgressDialog, QPushButton, QSpinBox, QSplitter,
    QStatusBar, QToolBar, QVBoxLayout, QWidget,
)

from tgdatabridge.core import ddl_generator
from tgdatabridge.core.assessment import build_assessment
from tgdatabridge.core.connector_factory import FILE_SOURCE_ENGINES, SOURCE_ENGINES, TARGET_ENGINES, introspector_for
from tgdatabridge.core.connector_factory import make_source_connector as _make_source_connector
from tgdatabridge.core.connector_factory import make_target_connector as _make_target_connector
from tgdatabridge.core.schema_model import Schema
from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.gui.ai_review_dialog import AiReviewDialog
from tgdatabridge.gui.ai_settings_dialog import AiSettingsDialog
from tgdatabridge.gui.connection_dialog import ConnectionDialog
from tgdatabridge.gui.history_dialog import HistoryDialog
from tgdatabridge.gui.log_console import LogConsole
from tgdatabridge import version
from tgdatabridge.gui.report_view import _POST_LOAD_PLACEHOLDER, ReportView
from tgdatabridge.gui.schema_tree import SchemaTree
from tgdatabridge.gui.settings_dialog import SettingsDialog
from tgdatabridge.gui.status_widgets import ConnectionBadge, StatusIndicator
from tgdatabridge.gui.target_schema_tree import TargetSchemaTree
from tgdatabridge.gui.watermark import DashboardWatermark, build_dashboard_watermark_pixmap
from tgdatabridge.reports.report_generator import generate_html_report
from tgdatabridge.utils import ai_settings, app_storage, crash, logger, metrics, resources
from tgdatabridge.utils import settings as app_settings

# Resolved through utils.resources rather than by path arithmetic off
# __file__ -- see that module: a frozen build whose _internal predates
# the rebrand keeps its assets under the old package directory name,
# and every load here tolerates a missing file, so getting this wrong
# costs the icon and the watermark with nothing said about it.
_ASSETS_DIR = resources.assets_dir()


class _CentralWidget(QWidget):
    """Plain QWidget subclass whose only job is keeping the watermark
    overlay's geometry in sync with its own size on resize -- the overlay
    isn't managed by the layout (it must float above every pane), so
    nothing else keeps it correctly sized automatically."""

    def __init__(self):
        super().__init__()
        self.watermark: Optional[QWidget] = None

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        super().resizeEvent(event)
        if self.watermark is not None:
            self.watermark.setGeometry(self.rect())


# Engine -> connector dispatch used to be two functions defined right here;
# it now lives in tgdatabridge.core.connector_factory (GUI-independent, also used
# by the headless CLI -- see ENTERPRISE_READINESS.md section 5, item 1)
# and is imported at the top of this file as _make_source_connector /
# _make_target_connector, so every existing call site below didn't need
# renaming.


class _PreflightBlocked:
    """The pre-flight found tables on the target that the script cannot
    be applied over. Carried back to the GUI thread instead of raised, so
    the user is offered the two ways out rather than an OK button. See
    MainWindow._offer_preflight_actions."""

    def __init__(self, problems):
        self.problems = list(problems)

    @property
    def table_names(self):
        return [p.table_name for p in self.problems]


class _ApplyFailed:
    """One statement was rejected and the run stopped there. Carried back
    to the GUI thread rather than raised, so the user can choose to see
    every remaining problem in one pass instead of rediscovering them one
    dialog at a time. See MainWindow._offer_continue_past_error."""

    def __init__(self, index: int, total: int, error: str, preview: str,
                 explanation: str = ""):
        self.index = index
        self.total = total
        self.error = error
        self.preview = preview
        #: A plain-language account of *why*, when the tool can work it
        #: out -- see tgdatabridge.core.fk_violations. Empty otherwise.
        self.explanation = explanation or ""


class _Worker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)
    # (done, total) -- emitted from inside the worker thread's own task
    # function via `self._worker.progress.emit(...)`; Qt marshals this to
    # the GUI thread automatically since sender and receiver live on
    # different threads (a queued connection), so this is safe to call
    # directly from task() closures without any extra locking.
    progress = Signal(int, int)
    # (table_name, rows_done, rows_total_estimate) -- the row-level
    # counterpart to `progress` above, which only ever reports whole
    # *tables* done/total. Migrate Data is the one caller that emits this
    # today (see MainWindow._emit_object_progress / _migrate_data), to
    # drive _MigrationProgressDialog's second, per-table progress bar --
    # every other _run_async caller simply never emits it, so connecting
    # this signal unconditionally in _run_async costs nothing for them.
    # rows_total_estimate is 0 when the source's row-count estimate for
    # that table isn't known (or is 0), which the receiving dialog treats
    # as "show an indeterminate bar" rather than a bogus 0/0 = 100%.
    object_progress = Signal(str, int, int)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            # str(exc) alone is what the user sees, and for an expected
            # failure (bad credentials, a rejected DDL statement) that is
            # the right amount of detail. But it used to be *all* that
            # survived: an unexpected error -- an AttributeError deep in a
            # converter, say -- reached the user as a bare message with no
            # file, line or stack, and nothing was written anywhere. The
            # traceback now goes to the log and the crash file (C2), while
            # the signal payload stays exactly as it was so no UI code
            # needed to change.
            try:
                detail = crash.format_exception(type(exc), exc, exc.__traceback__)
                logger.error(f"Background operation failed: {type(exc).__name__}: {exc}")
                for line in detail.rstrip().splitlines():
                    logger.error("  " + line)
                crash.record_handled_exception(detail)
            except Exception:  # noqa: BLE001
                pass
            self.failed.emit(str(exc))


class _MigrationProgressDialog(QDialog):
    """Migrate Data's own progress dialog -- everywhere else, _run_async's
    plain QProgressDialog (one bar: tables done/total) is exactly right,
    but Migrate Data is the one operation long and important enough to
    also want (a) a Pause button, so a person can hold a migration between
    batches without aborting and losing checkpoint context, and (b) a
    second bar showing *which* table is moving right now and how far
    through it the current batch stream is -- the thing that was actually
    missing when a user watched "8/26" sit still for fifteen minutes while
    a single LOB-heavy table streamed in the background (see
    migrator.migrate_table's per-batch `progress_cb`, which always had
    this information; nothing before this surfaced it in the GUI).

    Implements exactly the subset of QProgressDialog's API that
    MainWindow._run_async/_on_progress/_close_progress_dialog call
    (setWindowTitle, setWindowModality, setMinimumDuration, setCancelButton,
    show, setRange, setValue, setLabelText, close) so _run_async can accept
    this in place of a QProgressDialog via its `dialog_factory` parameter
    without any of that shared, generic code needing to know which kind of
    dialog it's driving. setMinimumDuration/setCancelButton are no-ops here
    on purpose: showing immediately is this dialog's only mode (there is no
    QProgressDialog-style "don't pop up for a fast operation" delay to
    configure), and there has never been a cancel button to preserve --
    Pause is this dialog's answer to "let me stop this for a moment",
    replacing the dead, always-disabled cancel button _run_async used to
    build for every operation.
    """

    _PAUSED_SUFFIX = " (paused)"

    def __init__(self, title: str, parent, pause_event: "threading.Event") -> None:
        super().__init__(parent)
        self._pause_event = pause_event
        self.setWindowTitle(title)
        # Window modality is set by _run_async right after construction
        # (WindowModal, same as every other operation's dialog) -- not
        # duplicated here.

        self._label = QLabel(title)
        self._overall_bar = QProgressBar()
        self._overall_bar.setRange(0, 0)

        self._object_label = QLabel("")
        self._object_bar = QProgressBar()
        self._object_bar.setRange(0, 0)

        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setToolTip(
            "Pause after the batch currently being written finishes -- the source and "
            "target connections stay open, and Resume continues from exactly where this "
            "left off. Nothing already written is undone."
        )
        self._pause_btn.clicked.connect(self._toggle_pause)

        layout = QVBoxLayout()
        layout.addWidget(self._label)
        layout.addWidget(self._overall_bar)
        layout.addWidget(self._object_label)
        layout.addWidget(self._object_bar)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self._pause_btn)
        layout.addLayout(btn_row)
        self.setLayout(layout)
        self.setMinimumWidth(420)

    # ---- QProgressDialog-compatible surface, called from _run_async -----

    def setMinimumDuration(self, _ms: int) -> None:  # noqa: N802 - Qt-style name, matches QProgressDialog
        pass

    def setCancelButton(self, _button) -> None:  # noqa: N802
        pass

    def setRange(self, lo: int, hi: int) -> None:  # noqa: N802
        self._overall_bar.setRange(lo, hi)

    def setValue(self, value: int) -> None:  # noqa: N802
        self._overall_bar.setValue(value)

    def setLabelText(self, text: str) -> None:  # noqa: N802
        self._label.setText(text)

    # ---------------------------------------------- the extra object bar

    def setObjectProgress(self, table_name: str, done: int, total: int) -> None:  # noqa: N802
        """`total` is a row-count *estimate* (Table.row_count_estimate,
        e.g. from Oracle's ALL_TABLES.NUM_ROWS) -- often stale, sometimes
        0/unknown for a table ANALYZE never ran on. A real, positive
        estimate gets a real percentage bar; anything else falls back to
        an indeterminate spinner with the row count spelled out in the
        label instead of showing a misleading 0/0 or 100% at row 1."""
        if total and total > 0:
            self._object_bar.setRange(0, total)
            self._object_bar.setValue(min(done, total))
            self._object_label.setText(f"Migrating: {table_name} ({done:,} / ~{total:,} rows)")
        else:
            self._object_bar.setRange(0, 0)
            self._object_label.setText(f"Migrating: {table_name} ({done:,} rows copied)")

    # -------------------------------------------------------- pause/resume

    def _toggle_pause(self) -> None:
        if self._pause_event.is_set():
            self._pause_event.clear()
            self._pause_btn.setText("Resume")
            if not self._label.text().endswith(self._PAUSED_SUFFIX):
                self._label.setText(self._label.text() + self._PAUSED_SUFFIX)
        else:
            self.force_resume()

    def force_resume(self) -> None:
        """Used both by the Resume button and by MainWindow.closeEvent --
        a migration left paused must never block the application from
        closing (or its worker thread waiting forever inside
        pause_event.wait() the whole 10s closeEvent already allows for)."""
        self._pause_event.set()
        self._pause_btn.setText("Pause")
        if self._label.text().endswith(self._PAUSED_SUFFIX):
            self._label.setText(self._label.text()[: -len(self._PAUSED_SUFFIX)])


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # The build stamp is in the title on purpose -- see tgdatabridge.version.
        self.setWindowTitle(version.title())
        self.resize(1200, 800)

        icon_path = _ASSETS_DIR / "logo_256.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.source_params: Optional[ConnectionParams] = None
        self.target_params: Optional[ConnectionParams] = None
        self.schema: Optional[Schema] = None
        # The subset of `self.schema` the last conversion actually ran on,
        # i.e. what the user still had ticked in the object tree when they
        # pressed "2. Convert Schema". Every step downstream of the
        # conversion -- Apply DDL's pre-flight, Dry Run, Migrate Data --
        # has to work from this rather than from the full loaded schema,
        # or it would check and copy tables the generated DDL never
        # created. None until the first conversion.
        self.active_schema: Optional[Schema] = None
        self._worker: Optional[_Worker] = None
        # Workers that have been superseded but may not have finished
        # unwinding yet. `self._worker = _Worker(...)` in _run_async used to
        # be the *only* Python reference to the previous QThread, so
        # reassigning it dropped the last reference; PySide then deleted the
        # underlying C++ QThread, and if that thread was still running Qt
        # calls std::terminate() -- "QThread: Destroyed while thread is
        # still running" -- which kills the process instantly, with no
        # traceback, no crash dialog and no log line. The window simply
        # vanishes.
        #
        # That is reachable on the ordinary success path: _apply_ddl_text's
        # on_success calls _refresh_target_schema, which starts a new worker
        # from inside the *old* worker's finished_ok slot, at a moment when
        # the old QThread has emitted its signal but not yet finished
        # run(). Holding retired workers here until Qt's own finished
        # signal fires closes that race for every operation at once.
        self._retired_workers: list = []

        self._build_toolbar()

        # Always-visible connection badges — shows "not connected" rather than
        # being blank/absent, so the current source/target state is never a
        # mystery. Sits in its own row right under the toolbar.
        self.source_badge = ConnectionBadge("Source")
        self.target_badge = ConnectionBadge("Target")
        badges_row = QWidget()
        badges_layout = QHBoxLayout(badges_row)
        badges_layout.setContentsMargins(8, 6, 8, 6)
        badges_layout.addWidget(self.source_badge)
        badges_layout.addWidget(self.target_badge)
        badges_layout.addStretch(1)

        self.schema_tree = SchemaTree()
        # Compact, read-only "what's actually on the target" pane -- sits
        # under the source tree, short by design (see TargetSchemaTree), and
        # refreshes automatically after Apply DDL / Migrate Data or on
        # demand via the toolbar's "Refresh Target Schema" button.
        self.target_schema_tree = TargetSchemaTree()
        self.report_view = ReportView()
        self.log_console = LogConsole()

        # The object tree's checkboxes decide what gets converted and
        # migrated, which is not obvious from a column of ticked boxes that
        # start out all ticked. This strip says so and makes the bulk
        # operations reachable without hunting for a context menu.
        selection_bar = QWidget()
        selection_layout = QHBoxLayout(selection_bar)
        selection_layout.setContentsMargins(6, 4, 6, 2)
        selection_layout.setSpacing(6)
        self.selection_label = QLabel("Untick an object to leave it out")
        self.selection_label.setToolTip(
            "Only ticked objects are converted, applied to the target and migrated.")
        selection_layout.addWidget(self.selection_label)
        selection_layout.addStretch(1)
        select_all_btn = QPushButton("Select all")
        select_all_btn.setToolTip("Tick every object in the schema.")
        select_all_btn.clicked.connect(lambda: self.schema_tree.set_all_checked(True))
        selection_layout.addWidget(select_all_btn)
        clear_all_btn = QPushButton("Clear all")
        clear_all_btn.setToolTip("Untick every object, then tick just the ones you want.")
        clear_all_btn.clicked.connect(lambda: self.schema_tree.set_all_checked(False))
        selection_layout.addWidget(clear_all_btn)
        invert_btn = QPushButton("Invert")
        invert_btn.clicked.connect(lambda: self.schema_tree.invert_selection())
        selection_layout.addWidget(invert_btn)

        source_pane = QWidget()
        source_layout = QVBoxLayout(source_pane)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(0)
        source_layout.addWidget(selection_bar)
        source_layout.addWidget(self.schema_tree)

        self.schema_tree.selection_changed.connect(self._update_selection_label)

        left_split = QSplitter(Qt.Orientation.Vertical)
        left_split.addWidget(source_pane)
        left_split.addWidget(self.target_schema_tree)
        left_split.setStretchFactor(0, 3)
        left_split.setStretchFactor(1, 1)

        top_split = QSplitter()
        top_split.addWidget(left_split)
        top_split.addWidget(self.report_view)
        top_split.setStretchFactor(0, 1)
        top_split.setStretchFactor(1, 3)

        main_split = QSplitter(Qt.Orientation.Vertical)
        main_split.addWidget(top_split)
        main_split.addWidget(self.log_console)
        main_split.setStretchFactor(0, 4)
        main_split.setStretchFactor(1, 1)

        container = _CentralWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(badges_row)
        layout.addWidget(main_split)

        # Faint Teleglobal logo watermark across the whole dashboard --
        # decoration only: click-through, ~11% opacity (see watermark.py's
        # _OPACITY for the exact value and change history), sized to a
        # physical ~10cm square. Raised above every pane so it's actually
        # visible rather than hidden behind their opaque backgrounds; its
        # low opacity keeps it from competing with them.
        watermark_pixmap = build_dashboard_watermark_pixmap(_ASSETS_DIR)
        self._watermark = DashboardWatermark(watermark_pixmap, parent=container)
        self._watermark.setGeometry(container.rect())
        container.watermark = self._watermark
        self._watermark.raise_()

        self.setCentralWidget(container)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(160)
        self.progress.setVisible(False)
        self.status_indicator = StatusIndicator()
        status = QStatusBar()
        status.addPermanentWidget(self.status_indicator)
        status.addPermanentWidget(self.progress)
        self.setStatusBar(status)

        self._progress_dialog: Optional[QProgressDialog] = None
        # Set only while a Migrate Data run is in flight (see _migrate_data
        # and _MigrationProgressDialog) -- lets closeEvent force-resume a
        # paused migration before waiting for its worker thread, so a
        # migration paused and forgotten about doesn't block the
        # application from closing for the whole 10s closeEvent otherwise
        # allows a wedged worker.
        self._migrate_pause_event: Optional["threading.Event"] = None

        # The scheduled incremental sync. `_sync_running` guards
        # against a tick arriving while the previous run is still
        # going: two syncs over the same tables at once would race on
        # the same rows and double-count what they moved. A tick that
        # arrives busy is skipped, not queued.
        self._sync_running = False
        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(lambda: self._sync_changes(scheduled=True))

        # (log shipping used to be applied here -- removed; logs are local only)
        # (Settings…) right away, so it's active for this whole session --
        # not just after the user happens to open the Settings dialog.

        logger.info(version.banner())
        # Says where the icons, watermark and stylesheet were found.
        # Every one of those loads tolerates a missing file, so an
        # unstyled window with no logo would otherwise be a silent
        # failure with nothing in the log to explain it.
        logger.info(resources.describe())
        logger.info("Ready. Connect a source database to begin.")

    # ------------------------------------------------------------------ UI

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.source_engine_combo = QComboBox()
        # Sourced from the factory rather than a second hardcoded list, so
        # adding an engine there can't leave the GUI silently out of date.
        self.source_engine_combo.addItems(list(SOURCE_ENGINES))
        self.source_engine_combo.setToolTip("Source engine")
        toolbar.addWidget(self.source_engine_combo)

        connect_source_btn = QPushButton("Connect Source")
        connect_source_btn.clicked.connect(self._connect_source)
        toolbar.addWidget(connect_source_btn)

        toolbar.addSeparator()

        self.target_engine_combo = QComboBox()
        self.target_engine_combo.addItems(list(TARGET_ENGINES))
        self.target_engine_combo.setToolTip("Target engine")
        toolbar.addWidget(self.target_engine_combo)

        connect_target_btn = QPushButton("Connect Target")
        connect_target_btn.clicked.connect(self._connect_target)
        toolbar.addWidget(connect_target_btn)

        toolbar.addSeparator()

        introspect_btn = QPushButton("1. Load Schema")
        introspect_btn.clicked.connect(self._load_schema)
        toolbar.addWidget(introspect_btn)

        convert_btn = QPushButton("2. Convert Schema")
        convert_btn.clicked.connect(self._convert_schema)
        toolbar.addWidget(convert_btn)

        self.defer_constraints_checkbox = QCheckBox("Defer constraints")
        self.defer_constraints_checkbox.setToolTip(
            "Hold primary keys, unique/check constraints, indexes and triggers back until after "
            "the data is migrated, then apply them with \"5. Apply Post-Load DDL\".\n\n"
            "Loading into a table with no indexes is substantially faster (nothing is maintained "
            "per row), building each index once over the finished table produces a denser index, "
            "and triggers never fire on migrated rows -- which is both faster and usually more "
            "correct, since the source rows already reflect whatever those triggers do.\n\n"
            "Trade-off: a constraint violation surfaces at the end of the run instead of on the "
            "first bad row, so run \"4a. Dry Run (Plan)\" first on anything large.\n\n"
            "Affects the next Convert Schema. See SCALE.md section 1.3."
        )
        toolbar.addWidget(self.defer_constraints_checkbox)

        # What to do when a foreign key is refused because the rows already
        # in the table break it -- the single most common way a real
        # migration stops one statement short of finished. See
        # tgdatabridge.core.fk_recovery for why the default is what it is.
        from tgdatabridge.core.fk_recovery import (
            POLICY_DELETE_ORPHANS, POLICY_NOT_VALID, POLICY_NULL_ORPHANS,
            POLICY_SKIP, POLICY_STOP)
        toolbar.addWidget(QLabel(" Orphan rows: "))
        self.fk_policy_combo = QComboBox()
        for policy, short in (
                (POLICY_NOT_VALID, "create the key anyway"),
                (POLICY_NULL_ORPHANS, "blank the orphans"),
                (POLICY_DELETE_ORPHANS, "delete the orphans"),
                (POLICY_SKIP, "leave the key off"),
                (POLICY_STOP, "stop and ask me")):
            self.fk_policy_combo.addItem(short, policy)
        self.fk_policy_combo.setCurrentIndex(0)
        self.fk_policy_combo.setToolTip(
            "What to do when the target refuses a foreign key because rows already in the "
            "table break it -- \"Key (team_id)=(...) is not present in table teams\".\n\n"
            "Those orphaned rows come from the source. MySQL enforces foreign keys only on "
            "InnoDB and only while FOREIGN_KEY_CHECKS is on, so a MyISAM table, a later "
            "conversion to InnoDB, or any bulk load done with the checks off leaves rows "
            "whose parent was deleted. The migration is usually the first thing that ever "
            "actually checks.\n\n"
            "create the key anyway (default) -- creates the constraint without re-checking "
            "the rows that are already there (PostgreSQL NOT VALID, Oracle ENABLE "
            "NOVALIDATE, SQL Server WITH NOCHECK, MySQL with the checks off for the length "
            "of the ALTER). Changes no data. Every row written from then on is checked. The "
            "orphans are counted and listed at the end, and one VALIDATE CONSTRAINT "
            "promotes the key to fully checked once you have cleaned them up.\n\n"
            "blank the orphans -- sets the offending foreign-key values to NULL, then "
            "creates the key fully checked. Only possible where the column allows NULL.\n\n"
            "delete the orphans -- deletes those child rows outright. Destructive.\n\n"
            "leave the key off -- carries on without that one constraint.\n\n"
            "stop and ask me -- the old behaviour: halt on the first one."
        )
        toolbar.addWidget(self.fk_policy_combo)

        apply_btn = QPushButton("3. Apply DDL to Target")
        apply_btn.clicked.connect(self._apply_ddl)
        toolbar.addWidget(apply_btn)

        dry_run_btn = QPushButton("4a. Dry Run (Plan)")
        dry_run_btn.setToolTip(
            "Check source row counts and target table readiness without writing any data -- "
            "safe to run repeatedly, including against a production source."
        )
        dry_run_btn.clicked.connect(self._dry_run_migration)
        toolbar.addWidget(dry_run_btn)

        migrate_btn = QPushButton("4b. Migrate Data")
        migrate_btn.setToolTip(
            "Migrates data table-by-table with automatic retry, post-migration row/checksum "
            "validation, and checkpointed resume -- re-running after a partial failure skips "
            "tables already fully copied and resumes the one that failed."
        )
        migrate_btn.clicked.connect(self._migrate_data)
        toolbar.addWidget(migrate_btn)

        post_load_btn = QPushButton("5. Apply Post-Load DDL")
        post_load_btn.setToolTip(
            "Applies the constraints, indexes and triggers that \"Defer constraints\" held back, "
            "now that the data is in place. Only meaningful if the schema was converted with that "
            "option ticked -- see the Post-Load DDL tab."
        )
        post_load_btn.clicked.connect(self._apply_post_load_ddl)
        toolbar.addWidget(post_load_btn)

        # Step 6: keep the target up to date after the bulk load. A
        # migration is almost never a single event -- the source keeps
        # working while the cutover is planned -- and re-running the whole
        # thing to move a few hundred changed rows is hours of work for
        # minutes of change. See tgdatabridge.core.incremental.
        toolbar.addSeparator()

        sync_now_btn = QPushButton("6. Sync Changes")
        sync_now_btn.setToolTip(
            "Bring the target back in line with the source: rows added, rows edited, and "
            "rows deleted since the last run.\n\n"
            "Per table it uses whichever is cheaper and safe. A table with an updated_at / "
            "modified / last_updated date-time column is read from the last run's "
            "high-water mark, so a 250,000-row table with 40 edits reads 40 rows. A table "
            "without one has every row compared against the target's own copy -- slower, "
            "but it catches an edit the application forgot to stamp.\n\n"
            "A table with no primary key is skipped and named: there is no way to say which "
            "target row a changed source row belongs to.\n\n"
            "Safe to run repeatedly. Run it after \"4b. Migrate Data\", not instead of it."
        )
        sync_now_btn.clicked.connect(lambda: self._sync_changes(scheduled=False))
        toolbar.addWidget(sync_now_btn)

        self.sync_deletes_checkbox = QCheckBox("incl. deletes")
        self.sync_deletes_checkbox.setChecked(True)
        self.sync_deletes_checkbox.setToolTip(
            "Also remove rows from the target that have been deleted at the source, so the "
            "target is a true mirror.\n\n"
            "A deleted row leaves nothing behind to carry a timestamp, so the only way to "
            "find one is to read the target's keys and ask the source whether each still "
            "exists -- one pass over the keys of every table, whichever detection method "
            "found the changed rows.\n\n"
            "Untick it if the target deliberately keeps history the source has purged."
        )
        toolbar.addWidget(self.sync_deletes_checkbox)

        toolbar.addWidget(QLabel(" every "))
        self.sync_interval_spin = QSpinBox()
        self.sync_interval_spin.setRange(1, 1440)
        self.sync_interval_spin.setValue(15)
        self.sync_interval_spin.setSuffix(" min")
        self.sync_interval_spin.setToolTip(
            "How often the scheduled sync repeats. A tick that arrives while the previous "
            "run is still going is skipped rather than queued, so a long sync cannot pile "
            "up behind itself."
        )
        toolbar.addWidget(self.sync_interval_spin)

        self.sync_schedule_btn = QPushButton("Start scheduled sync")
        self.sync_schedule_btn.setToolTip(
            "Repeat the sync on that interval until stopped, so the target stays close to "
            "live while a cutover is planned. The first run starts immediately.\n\n"
            "Scheduled runs report to the log and the status bar rather than opening a "
            "dialog -- a sync every fifteen minutes that interrupts what you are doing is a "
            "sync nobody leaves running. A failure is still shown."
        )
        self.sync_schedule_btn.clicked.connect(self._toggle_sync_schedule)
        toolbar.addWidget(self.sync_schedule_btn)

        toolbar.addWidget(QLabel(" Parallel workers: "))
        self.max_workers_spin = QSpinBox()
        self.max_workers_spin.setRange(1, 32)
        self.max_workers_spin.setValue(4)
        self.max_workers_spin.setToolTip(
            "How many tables to migrate at once during Migrate Data, within each "
            "FK-dependency \"wave\" (a table is never migrated before another table its own "
            "foreign keys reference, no matter how high this is set). 4 (default) gives a "
            "meaningful speed-up on most schemas without overwhelming the source/target "
            "connection limits; set it to 1 for the original single-threaded behavior, or "
            "higher on a large schema with plenty of headroom. See ENTERPRISE_READINESS.md "
            "section 4."
        )
        toolbar.addWidget(self.max_workers_spin)

        toolbar.addSeparator()

        rollback_btn = QPushButton("Rollback Script")
        rollback_btn.setToolTip("Generate a DROP script that undoes the converted schema's DDL.")
        rollback_btn.clicked.connect(self._generate_rollback)
        toolbar.addWidget(rollback_btn)

        apply_rollback_btn = QPushButton("Reset Target…")
        apply_rollback_btn.setToolTip(
            "Drop this schema's objects from the target so \"Apply DDL to Target\" can start "
            "from a clean database. Asks for confirmation first."
        )
        apply_rollback_btn.clicked.connect(self._apply_rollback)
        toolbar.addWidget(apply_rollback_btn)

        save_report_btn = QPushButton("Save Report...")
        save_report_btn.clicked.connect(self._save_report)
        toolbar.addWidget(save_report_btn)

        save_ddl_btn = QPushButton("Save DDL...")
        save_ddl_btn.clicked.connect(self._save_ddl)
        toolbar.addWidget(save_ddl_btn)

        save_post_load_btn = QPushButton("Save Post-Load DDL...")
        save_post_load_btn.setToolTip(
            "Save the post-load script. With \"Defer constraints\" ticked this is where every "
            "trigger, index and foreign key lives -- \"Save DDL...\" does not include them.")
        save_post_load_btn.clicked.connect(self._save_post_load_ddl)
        toolbar.addWidget(save_post_load_btn)

        save_rollback_btn = QPushButton("Save Rollback...")
        save_rollback_btn.clicked.connect(self._save_rollback)
        toolbar.addWidget(save_rollback_btn)

        toolbar.addSeparator()

        refresh_target_btn = QPushButton("Refresh Target Schema")
        refresh_target_btn.clicked.connect(self._refresh_target_schema)
        toolbar.addWidget(refresh_target_btn)

        toolbar.addSeparator()

        history_btn = QPushButton("History…")
        history_btn.clicked.connect(self._show_history)
        toolbar.addWidget(history_btn)

        settings_btn = QPushButton("Settings…")
        settings_btn.setToolTip("Shared storage folder for a team deployment.")
        settings_btn.clicked.connect(self._show_settings)
        toolbar.addWidget(settings_btn)

        toolbar.addSeparator()

        ai_settings_btn = QPushButton("AI Settings…")
        ai_settings_btn.setToolTip(
            "Configure the optional AI-assisted features (mapping review, plain-English "
            "requests, error diagnosis, data quality review). Off by default.")
        ai_settings_btn.clicked.connect(self._show_ai_settings)
        toolbar.addWidget(ai_settings_btn)

        ai_review_btn = QPushButton("AI Review…")
        ai_review_btn.setToolTip(
            "Schema mapping review, plain-English migration requests, and post-migration "
            "data quality review. Requires AI Settings to be turned on first.")
        ai_review_btn.clicked.connect(self._show_ai_review)
        toolbar.addWidget(ai_review_btn)

    # ------------------------------------------------------------- actions

    def _fk_policy(self) -> str:
        """The user's choice for a foreign key whose rows break it.

        Read through a method, and defensively, because the apply worker
        runs on a background thread: it must not touch a widget that a
        headless test or a partially-built window has not created, and it
        must not be the thing that breaks an apply if the combo is ever
        renamed. Anything unrecognised falls back to the safe default,
        which changes no data.
        """
        from tgdatabridge.core.fk_recovery import POLICIES, POLICY_NOT_VALID
        combo = getattr(self, "fk_policy_combo", None)
        if combo is None:
            return POLICY_NOT_VALID
        try:
            value = combo.currentData()
        except Exception:  # noqa: BLE001
            return POLICY_NOT_VALID
        return value if value in POLICIES else POLICY_NOT_VALID

    def _connect_source(self) -> None:
        engine = self.source_engine_combo.currentText()
        dialog = ConnectionDialog(engine, self, role="source")
        if dialog.exec():
            self.source_params = dialog.params()
            # MySQL and MongoDB have no separate "schema" field in the
            # connection dialog (their schema concept is the database
            # itself, already entered there) -- Oracle's schema defaults
            # to the connecting username when left blank, same fallback
            # ConnectionDialog.params() and OracleConnector.schema_name
            # already use.
            if engine in FILE_SOURCE_ENGINES:
                # There's no host/port/database here at all -- `database`
                # holds a file path. Show the file name as the "database"
                # and let the connector's own fallback (the file's stem)
                # decide the schema when the field was left blank.
                path = Path(self.source_params.database)
                schema_display = self.source_params.schema or path.stem
                self.source_badge.set_connected(engine, "local file", path.name, schema_display)
                logger.info(f"Source configured: {engine} file {self.source_params.database} "
                            f"(schema {schema_display})")
                return
            if engine in ("MySQL", "MongoDB"):
                schema_display = self.source_params.database
            else:
                schema_display = self.source_params.schema or self.source_params.username
            self.source_badge.set_connected(
                engine, self.source_params.host, self.source_params.database, schema_display)
            logger.info(f"Source configured: {engine} at {self.source_params.host}:{self.source_params.port}/"
                        f"{self.source_params.database} (schema {schema_display})")

    def _connect_target(self) -> None:
        engine = self.target_engine_combo.currentText()
        dialog = ConnectionDialog(engine, self)
        if dialog.exec():
            self.target_params = dialog.params()
            self.target_badge.set_connected(
                engine, self.target_params.host, self.target_params.database, self.target_params.schema)
            logger.info(f"Target configured: {engine} at {self.target_params.host}:{self.target_params.port}/"
                        f"{self.target_params.database}"
                        + (f" (schema: {self.target_params.schema})" if self.target_params.schema else ""))

    def _run_async(self, label: str, fn, on_success, *args, metrics_extra=None,
                   silent: bool = False, on_failed=None,
                   dialog_factory: Optional[Callable[[], QWidget]] = None, **kwargs) -> None:
        # `on_failed`, when given, runs after the failure dialog. It is
        # for state a caller has to unwind either way -- the scheduled
        # sync's "a run is in progress" flag, which would otherwise stay
        # set forever after one failed tick and silently stop every
        # later one.
        # metrics_extra, if given, is called with the task's result once it
        # succeeds and should return a dict of extra Prometheus-label-style
        # fields (e.g. {"source_engine": ..., "rows": ...}) to attach to
        # this operation's metrics record -- see tgdatabridge.utils.metrics and
        # ENTERPRISE_READINESS.md section 3, item 4. Every operation gets a
        # duration + success/failure metric regardless of whether a caller
        # passes this; it only adds operation-specific dimensions on top.
        # `dialog_factory`, when given, replaces the plain QProgressDialog
        # below with whatever it returns -- used by Migrate Data to show
        # _MigrationProgressDialog (Pause button + a second, per-table
        # progress bar) instead. The factory's result only needs to
        # implement the same handful of QProgressDialog methods called
        # here and in _on_progress/_close_progress_dialog; every other
        # caller leaves this unset and gets the original dialog, unchanged.
        self.status_indicator.set_running(label)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)

        # `silent` is for the follow-up refresh that runs by itself after
        # Apply DDL or Migrate Data. Those are long operations, and
        # answering one with a *second* modal dialog reads as "still
        # working" long after the work the user asked for has finished --
        # the status bar and the log already say what is happening.
        # Pressing "Refresh Target Schema" deliberately still shows one.
        if silent:
            self._progress_dialog = None
        else:
            dialog = dialog_factory() if dialog_factory is not None else QProgressDialog(f"{label}…", None, 0, 0, self)
            dialog.setWindowTitle(version.PRODUCT_TM)
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            dialog.setMinimumDuration(0)  # show immediately, don't wait ~4s before popping up
            dialog.setCancelButton(None)  # not cancellable mid-flight; avoid a dead button
            dialog.show()
            self._progress_dialog = dialog

        start_time = time.monotonic()
        previous = self._worker
        if previous is not None and previous.isRunning():
            # Never drop the last reference to a live QThread -- see
            # _retired_workers in __init__ for why that is fatal rather than
            # merely untidy.
            self._retired_workers.append(previous)
            previous.finished.connect(lambda w=previous: self._reap_worker(w))
        worker = _Worker(fn, *args, **kwargs)
        self._worker = worker
        worker.finished_ok.connect(
            lambda result: self._on_async_done(label, on_success, result, start_time, metrics_extra))
        worker.object_progress.connect(
            lambda name, done, total: self._on_object_progress(name, done, total))
        worker.failed.connect(
            lambda message: self._on_async_failed(label, message, start_time, on_failed))
        worker.progress.connect(lambda done, total: self._on_progress(label, done, total))
        worker.start()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        """Wait for background work before the window (and its workers) are
        destroyed.

        Without this, quitting while an operation is in flight tears down
        the MainWindow -- and with it the last reference to a running
        QThread -- which Qt answers with std::terminate(). The user sees the
        application die on exit rather than close, and the only trace is a
        faulthandler dump rather than anything Python could report."""
        # A paused Migrate Data run has its worker thread blocked inside
        # pause_event.wait() -- indistinguishable, from here, from a truly
        # wedged worker. Force it running again first, or a migration
        # paused and forgotten about eats the entire 10s wait below for
        # nothing and then still gets killed mid-batch anyway.
        if self._migrate_pause_event is not None:
            self._migrate_pause_event.set()
        workers = [w for w in ([self._worker] + list(self._retired_workers)) if w is not None]
        running = [w for w in workers if w.isRunning()]
        if running:
            logger.info("Waiting for %d background operation(s) to finish before closing..."
                        % len(running))
            for worker in running:
                # Bounded: a driver wedged on a network call must not make
                # the window unclosable.
                worker.wait(10000)
        # Any SSH tunnel opened for a bastion connection is a live socket
        # and, with the OpenSSH fallback, a child process. Leaving either
        # behind would keep a forwarded port open after the window is
        # gone. See tgdatabridge/db/ssh_tunnel.py.
        try:
            from tgdatabridge.db.ssh_tunnel import tunnels
            tunnels.close_all()
        except Exception:  # noqa: BLE001 - never block closing over cleanup
            pass
        super().closeEvent(event)

    def _emit_progress(self, done: int, total: int) -> None:
        """Report progress from inside a worker thread.

        Called from task() closures, which run on the worker. Reading
        `self._worker` directly there could pick up a *different* worker if
        another operation started meanwhile, and could be None between
        operations -- an AttributeError raised inside the thread, which
        surfaces to the user as the whole operation failing for no visible
        reason. Progress reporting is cosmetic and must never do that."""
        worker = self._worker
        if worker is None:
            return
        try:
            worker.progress.emit(done, total)
        except Exception:  # noqa: BLE001 -- cosmetic only, never fail the task
            pass

    def _emit_object_progress(self, table_name: str, done: int, total: int) -> None:
        """Row-level counterpart to _emit_progress -- see _Worker.object_progress
        and _MigrationProgressDialog.setObjectProgress. Same
        worker-may-have-changed-or-be-gone guard and the same
        never-fail-the-task swallow, for the same reason: this is cosmetic
        progress reporting from inside a background thread, not something
        a transient signal-delivery hiccup should ever be allowed to turn
        into a failed migration."""
        worker = self._worker
        if worker is None:
            return
        try:
            worker.object_progress.emit(table_name, done, total)
        except Exception:  # noqa: BLE001 -- cosmetic only, never fail the task
            pass

    def _on_object_progress(self, table_name: str, done: int, total: int) -> None:
        # Only _MigrationProgressDialog implements setObjectProgress; the
        # plain QProgressDialog every other _run_async caller uses does
        # not, so this is a silent no-op for every operation except
        # Migrate Data -- exactly the point of keeping _run_async itself
        # generic rather than special-casing "Migrate Data" by name there.
        dialog = self._progress_dialog
        if dialog is None:
            return
        setter = getattr(dialog, "setObjectProgress", None)
        if setter is None:
            return
        try:
            setter(table_name, done, total)
        except Exception:  # noqa: BLE001 -- cosmetic only, never fail the task
            pass

    def _reap_worker(self, worker) -> None:
        """Drop a retired worker once Qt says its thread has finished."""
        try:
            worker.wait(50)
        except Exception:  # noqa: BLE001
            pass
        if worker in self._retired_workers:
            self._retired_workers.remove(worker)

    def _on_progress(self, label: str, done: int, total: int) -> None:
        # Switches the status bar's progress bar and the modal dialog from
        # an indeterminate spinner to a real N/total count the first time
        # a task reports one -- large operations (tens of thousands of DDL
        # statements or tables) otherwise give no sense of how far along
        # they are.
        if total <= 0:
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)

        # QProgressDialog.setValue() calls QApplication.processEvents()
        # internally whenever the dialog is modal -- and this one is
        # (WindowModal, set in _run_async). That means the worker's queued
        # finished_ok/failed signal can be delivered *inside* the setValue
        # call below, running _on_async_done -> _close_progress_dialog,
        # which sets self._progress_dialog to None. The next line then
        # dereferenced None:
        #
        #   AttributeError: 'NoneType' object has no attribute 'setLabelText'
        #
        # A plain "is not None" check at the top can't prevent that: the
        # attribute is still set when the check runs and gone by the time
        # the last line executes. So take a local reference (which keeps
        # the dialog alive and makes a None dereference impossible) and
        # re-check afterwards rather than touching a dialog the app has
        # already finished with.
        dialog = self._progress_dialog
        if dialog is None:
            return
        if getattr(self, "_in_progress_update", False):
            # QProgressDialog.setValue() on a *modal* dialog calls
            # QApplication.processEvents() internally, which can deliver the
            # next queued progress signal while we are still inside this
            # call. Left unguarded, a fast operation reporting thousands of
            # steps re-enters here once per step and nests native stack
            # frames until the C stack overflows -- a hard crash with no
            # Python traceback, indistinguishable from the application
            # simply closing itself.
            return
        self._in_progress_update = True
        try:
            dialog.setRange(0, total)
            dialog.setValue(done)
        finally:
            self._in_progress_update = False
        if self._progress_dialog is not dialog:
            # The operation completed while we were inside setValue. The
            # dialog is closed and its final state no longer matters;
            # calling setLabelText on it now would at best be pointless
            # and at worst re-show a dialog nothing will close.
            return
        dialog.setLabelText(f"{label}… ({done}/{total})")

    def _close_progress_dialog(self) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
            self._progress_dialog = None

    def _on_async_done(self, label: str, on_success, result, start_time=None, metrics_extra=None) -> None:
        self.progress.setVisible(False)
        self._close_progress_dialog()
        self.status_indicator.set_success(label)
        self._record_operation_metrics(label, start_time, True, result=result, metrics_extra=metrics_extra)
        on_success(result)

    def _on_async_failed(self, label: str, message: str, start_time=None,
                         on_failed=None) -> None:
        self.progress.setVisible(False)
        self._close_progress_dialog()
        self.status_indicator.set_failed(label)
        self._record_operation_metrics(label, start_time, False)
        logger.error(message)
        if on_failed is not None:
            try:
                on_failed(message)
            except Exception:  # noqa: BLE001 - cleanup must not mask the failure
                pass
        QMessageBox.critical(self, "Operation failed", message)

    def _record_operation_metrics(self, label: str, start_time, success: bool, result=None, metrics_extra=None) -> None:
        # Metrics are purely observational -- any problem building or
        # persisting them must never surface as (or be mistaken for) a
        # failure of the actual operation that just ran.
        try:
            duration = time.monotonic() - start_time if start_time is not None else 0.0
            extra = {}
            if success and metrics_extra is not None:
                extra = metrics_extra(result) or {}
            metrics.record_operation(label, duration, success, actor=logger.current_actor(), **extra)
        except Exception:
            pass

    # ------------------------------------------------- object selection

    def _update_selection_label(self) -> None:
        """Keep the strip above the tree honest about what a run will
        actually touch."""
        if not self.schema:
            self.selection_label.setText("Untick an object to leave it out")
            return
        excluded = self.schema_tree.excluded_summary()
        if not excluded:
            self.selection_label.setText("All objects selected")
            self.selection_label.setToolTip(
                "Only ticked objects are converted, applied to the target and migrated.")
            return
        total_excluded = sum(int(line.split(":")[1].strip().split(" ")[0])
                             for line in excluded)
        self.selection_label.setText(
            f"<b>{total_excluded} object(s) left out</b> of the next conversion")
        self.selection_label.setToolTip("\n".join(excluded))

    def _selected_schema(self):
        """The loaded schema narrowed to whatever is still ticked, plus the
        notes to log. Returns (None, []) when nothing at all is selected."""
        from tgdatabridge.core.schema_subset import subset_schema

        if not self.schema_tree.has_any_checked():
            return None, []
        subset, notes = subset_schema(
            self.schema,
            tables=self.schema_tree.checked_objects("Tables"),
            views=self.schema_tree.checked_objects("Views"),
            sequences=self.schema_tree.checked_objects("Sequences"),
            routines=self.schema_tree.checked_objects("Routines / Triggers"),
        )
        return subset, notes

    def _migration_tables(self):
        """Which tables the data steps should work on: the ones the last
        conversion actually created, falling back to the current ticks
        before any conversion has run."""
        if self.active_schema is not None:
            return self.active_schema.tables
        checked = self.schema_tree.checked_objects("Tables")
        return checked if checked else self.schema.tables

    # ------------------------------------------------- incremental sync

    def _sync_id(self) -> Optional[str]:
        """The identity the high-water marks are stored under -- the same
        four inputs a full migration checkpoints by, so the marks belong
        to one logical source -> target -> schema pairing and are found
        again the next time the app runs."""
        if not (self.source_params and self.target_params and self.schema):
            return None
        return app_storage.checkpoint_id_for(
            self.source_params.database, self.target_params.database,
            self.schema.name, self.target_engine_combo.currentText())

    def _sync_changes(self, scheduled: bool = False) -> None:
        """Bring the target back in line with the source.

        `scheduled` is True for a repeat fired by the timer, and suppresses
        the modal result dialog -- a sync every fifteen minutes that
        interrupts whatever the user is doing with an OK box is a sync
        nobody leaves running. The log and the status bar still report
        every run, and a failure is still shown.
        """
        if not (self.source_params and self.target_params and self.schema):
            if not scheduled:
                QMessageBox.warning(
                    self, "Prerequisites missing",
                    "Connect the source and target and load the schema first. A sync only "
                    "makes sense after a full migration has already put the rows there.")
            return
        if self._sync_running:
            logger.info("Sync Changes: a sync is already running; this tick was skipped.")
            return

        source_engine = self.source_engine_combo.currentText()
        target_engine = self.target_engine_combo.currentText()
        tables = self._migration_tables()
        sync_id = self._sync_id()
        detect_deletes = self.sync_deletes_checkbox.isChecked()
        marks = app_storage.load_watermarks(sync_id) if sync_id else {}

        def task():
            from tgdatabridge.core.incremental import sync_schema, watermarks_from
            source = _make_source_connector(source_engine, self.source_params)
            target = _make_target_connector(target_engine, self.target_params)
            source.connect()
            try:
                target.connect()
                try:
                    report = sync_schema(
                        source, target, tables, watermarks=marks,
                        detect_deletes=detect_deletes)
                    return report, watermarks_from(report, marks)
                finally:
                    target.close()
            finally:
                source.close()

        def on_success(result):
            report, new_marks = result
            self._sync_running = False
            if sync_id:
                # Saved only after the run finished: a mark advanced past
                # rows that were read but never written would make those
                # changes invisible to every later sync.
                app_storage.save_watermarks(sync_id, new_marks)
            logger.info(f"Sync Changes: {report.headline()}")
            for line in report.results:
                if line.changed or line.error or line.skipped:
                    logger.info(f"  {line.summary()}")
            self.status_indicator.set_success(report.headline())
            if report.failed:
                QMessageBox.warning(
                    self, "Sync finished with failures",
                    f"{report.headline()}\n\n"
                    f"{len(report.failed)} table(s) failed:\n  "
                    + "\n  ".join(
                        r.summary() for r in report.results if r.error)[:2000])
                return
            if not scheduled:
                note = "\n\n".join(
                    r.summary() for r in report.results if r.changed or r.skipped)
                QMessageBox.information(
                    self, "Sync complete",
                    report.headline() + (f"\n\n{note}" if note else ""))

        self._sync_running = True
        self._run_async("Sync Changes", task, on_success, silent=scheduled,
                        on_failed=lambda _message: setattr(self, "_sync_running", False))

    def _toggle_sync_schedule(self) -> None:
        """Start or stop repeating the sync on a timer."""
        if self._sync_timer.isActive():
            self._sync_timer.stop()
            self.sync_schedule_btn.setText("Start scheduled sync")
            logger.info("Scheduled sync stopped.")
            self.status_indicator.set_success("Scheduled sync stopped.")
            return
        minutes = self.sync_interval_spin.value()
        self._sync_timer.start(minutes * 60 * 1000)
        self.sync_schedule_btn.setText("Stop scheduled sync")
        logger.info(f"Scheduled sync started: every {minutes} minute(s). "
                    f"The first run starts now.")
        self._sync_changes(scheduled=True)

    def _load_schema(self) -> None:
        if not self.source_params:
            QMessageBox.warning(self, "No source connection", "Connect to the source database first.")
            return

        source_engine = self.source_engine_combo.currentText()

        def task():
            # Same dispatch the headless CLI uses (tgdatabridge/cli/runner.py) --
            # this used to be an inline if/elif chain here, which is
            # exactly the GUI-vs-CLI drift connector_factory exists to
            # prevent.
            introspect_schema = introspector_for(source_engine)
            conn = _make_source_connector(source_engine, self.source_params)
            conn.connect()
            try:
                schema_name = conn.schema_name
                logger.info(f"Reading schema '{schema_name}' from {source_engine}...")
                schema = introspect_schema(conn, schema_name)
                return schema
            finally:
                conn.close()

        def on_success(schema: Schema):
            self.schema = schema
            self.schema.target_engine = self.target_engine_combo.currentText()
            # A fresh load is a new object graph: start with everything
            # ticked rather than carrying the previous schema's exclusions
            # onto objects that merely share a name.
            self.active_schema = None
            self.schema_tree.load_schema(schema, converted=False,
                                         preserve_selection=False)
            self._update_selection_label()
            # Any previously-computed diff was against a different (or
            # differently-scoped) source load -- stale until the target is
            # refreshed again against this one.
            self.report_view.clear_diff()
            logger.info(
                f"Loaded {len(schema.tables)} tables, {len(schema.views)} views, "
                f"{len(schema.sequences)} sequences, {len(schema.routines)} routines/triggers."
            )

        logger.info(f"Loading schema from {source_engine}...")
        self._run_async("Load Schema", task, on_success)

    def _convert_schema(self) -> None:
        if not self.schema:
            QMessageBox.warning(self, "No schema loaded", "Load the source schema first (step 1).")
            return

        target_engine = self.target_engine_combo.currentText()
        self.schema.target_engine = target_engine
        target_schema = self.target_params.schema if self.target_params else None

        # Only what is still ticked in the object tree. Unticking used to
        # affect nothing but the data-migration step -- the DDL was always
        # generated for the whole loaded schema -- so an object the user
        # had explicitly excluded still got created on the target.
        selected, notes = self._selected_schema()
        if selected is None:
            QMessageBox.warning(
                self, "Nothing selected",
                "Every object in the tree is unticked, so there is nothing to convert.\n\n"
                "Tick the objects you want to migrate, or use \"Select all\".")
            return
        selected.target_engine = target_engine
        excluded = self.schema_tree.excluded_summary()
        if excluded:
            logger.warning("Converting a subset of the schema:")
            for line in excluded:
                logger.warning(f"  {line}")
        for note in notes:
            logger.warning(f"  {note}")

        defer = self.defer_constraints_checkbox.isChecked()

        def task():
            def on_progress(done, total):
                self._emit_progress(done, total)

            # ddl_generator.generate_schema_ddl converts routines/triggers to PL/pgSQL
            # (for a PostgreSQL target) as part of building the combined DDL script.
            if defer:
                # Two-phase: constraints, indexes and triggers are held back
                # for "5. Apply Post-Load DDL" -- see SCALE.md section 1.3.
                ddl_text, post_load_text, issues = ddl_generator.generate_schema_ddl_phased(
                    selected, target_engine, target_schema, progress_cb=on_progress)
            else:
                ddl_text, issues = ddl_generator.generate_schema_ddl(
                    selected, target_engine, target_schema, progress_cb=on_progress)
                post_load_text = ""
            summary = build_assessment(selected)
            report_html = generate_html_report(selected, summary)
            return ddl_text, post_load_text, report_html, summary

        def on_success(result):
            ddl_text, post_load_text, report_html, summary = result
            self.report_view.set_ddl(ddl_text)
            self.report_view.set_post_load_ddl(post_load_text)
            self.report_view.set_report(report_html)
            if post_load_text:
                logger.info(
                    "Constraints, indexes and triggers deferred to the Post-Load DDL tab -- "
                    "run \"5. Apply Post-Load DDL\" after migrating data."
                )
            # The subset shares its objects with self.schema, so the
            # tree shows every loaded object with the conversion status
            # the run just produced -- a table trimmed of a dangling
            # foreign key is the one copy, and is carried across here.
            from tgdatabridge.core.schema_subset import sync_conversion_results
            sync_conversion_results(selected, self.schema)
            self.active_schema = selected
            self.schema_tree.load_schema(self.schema, converted=True)
            self._update_selection_label()
            logger.info(
                f"Conversion complete: {summary.automatic_pct}% automatic, "
                f"~{summary.estimated_manual_hours}h estimated manual effort, "
                f"{len(summary.action_items)} action item(s)."
            )
            try:
                app_storage.record_conversion_run(
                    schema_name=self.schema.name,
                    source_engine=self.schema.source_engine,
                    source_database=self.source_params.database if self.source_params else "",
                    target_engine=target_engine,
                    target_database=self.target_params.database if self.target_params else "",
                    target_schema=target_schema,
                    total_objects=summary.total_objects,
                    automatic_pct=summary.automatic_pct,
                    estimated_manual_hours=summary.estimated_manual_hours,
                    action_item_count=len(summary.action_items),
                    report_html=report_html,
                    ddl_text=ddl_text,
                    actor=logger.current_actor(),
                )
            except OSError as exc:
                # Recording history is a convenience, not something that
                # should ever surface as a failed conversion over a
                # disk/permissions problem.
                logger.warning(f"Could not record this run to conversion history: {exc}")

        def convert_metrics(result):
            # task() returns a 4-tuple; this used to unpack three, so every
            # conversion raised ValueError inside _record_operation_metrics
            # and silently recorded nothing but the bare duration.
            _ddl_text, _post_load_text, _report_html, summary = result
            return {
                "source_engine": self.schema.source_engine,
                "target_engine": target_engine,
                "total_objects": summary.total_objects,
                "automatic_pct": summary.automatic_pct,
                "action_items": len(summary.action_items),
            }

        logger.info(f"Converting schema to {target_engine}...")
        self._run_async("Convert Schema", task, on_success, metrics_extra=convert_metrics)

    def _show_history(self) -> None:
        dialog = HistoryDialog(self)
        dialog.exec()

    def _show_settings(self) -> None:
        dialog = SettingsDialog(self)
        if dialog.exec():
            logger.info("Settings saved.")

    def _show_ai_settings(self) -> None:
        dialog = AiSettingsDialog(self)
        if dialog.exec():
            logger.info(f"AI Settings saved: {ai_settings.to_ai_config(dialog.result_settings()).describe()}")

    def _show_ai_review(self) -> None:
        settings = ai_settings.load_ai_settings()
        if not settings.enabled:
            QMessageBox.information(
                self, "AI Review",
                "AI features are off. Turn them on in AI Settings first.")
            return
        if not self.schema:
            QMessageBox.information(
                self, "AI Review", "Load a source schema first (step 1).")
            return
        dialog = AiReviewDialog(self.schema, self.target_engine_combo.currentText(),
                                 self.target_params, self)
        dialog.exec()

    def _apply_ddl(self) -> None:
        self._apply_ddl_text(
            self.report_view.ddl_view.toPlainText(), "Apply DDL to Target",
            empty_hint="Convert the schema first (step 2).",
        )

    def _apply_post_load_ddl(self) -> None:
        """Step 5: apply the constraints, indexes and triggers that
        "Defer constraints" held back, once the data is in place (SCALE.md
        section 1.3). A no-op unless the schema was converted with
        deferral on."""
        text = self.report_view.post_load_view.toPlainText()
        if text.strip() == _POST_LOAD_PLACEHOLDER.strip():
            QMessageBox.information(
                self, "Nothing deferred",
                "This schema was converted with \"Defer constraints\" off, so the DDL already "
                "applied in step 3 included every constraint, index and trigger. There is nothing "
                "left to apply.",
            )
            return
        self._apply_ddl_text(
            text, "Apply Post-Load DDL",
            empty_hint="Convert the schema with \"Defer constraints\" ticked first (step 2).",
        )

    def _apply_ddl_text(self, ddl_text: str, label: str, empty_hint: str,
                        preflight: bool = True, continue_on_error: bool = False,
                        then=None) -> None:
        """Apply a script statement by statement.

        `preflight=False` skips the "does the target already contain a
        conflicting table" check -- correct for the rollback script, whose
        entire purpose is to remove those tables.

        `continue_on_error=True` reports every failure at the end instead
        of stopping at the first. Right for a cleanup pass, where an object
        that isn't there is not a problem; wrong for applying DDL, where
        statement N+1 usually depends on statement N having worked.

        `then`, if given, is called after a successful run -- used to
        chain "drop the conflicting tables" into "now apply the DDL".
        """
        if not self.target_params:
            QMessageBox.warning(self, "No target connection", "Connect to the target database first.")
            return
        if not ddl_text.strip():
            QMessageBox.warning(self, "Nothing to apply", empty_hint)
            return

        engine = self.target_engine_combo.currentText()
        # Read on the GUI thread and captured, not read from inside task():
        # task() runs on a QThread, and reading a widget from a non-GUI
        # thread is exactly the class of bug that ends in std::terminate().
        fk_policy = self._fk_policy()

        def task():
            target = _make_target_connector(engine, self.target_params)
            target.connect()
            try:
                # Before running anything: a table that already exists with
                # a different shape makes CREATE TABLE IF NOT EXISTS a
                # silent no-op, and the failure then surfaces several
                # statements later as a foreign-key error naming the
                # constraint instead of the stale table -- with the schema
                # left half-applied. See tgdatabridge.core.target_shape.
                check_schema = self.active_schema or self.schema
                if preflight and check_schema is not None and check_schema.tables:
                    from tgdatabridge.core.target_shape import check_before_apply_ddl
                    problems = check_before_apply_ddl(
                        target, check_schema.tables,
                        schema=getattr(target, "schema_name", None))
                    if problems:
                        # Returned rather than raised: an error dialog with
                        # only an OK button leaves the user stuck, and the
                        # two things they need -- leave those tables out,
                        # or replace them on the target -- are both things
                        # this tool can do for them. See
                        # _offer_preflight_actions.
                        return _PreflightBlocked(problems)

                from tgdatabridge.utils.sql_split import has_executable_sql, split_sql_statements
                all_statements = split_sql_statements(ddl_text)
                # Comment-only blocks (a "MANUAL CONVERSION REQUIRED"
                # placeholder wrapping the original routine source) stay in
                # the script the user reads and saves, but must never be sent
                # to a server: MySQL answers with "Query was empty" and
                # psycopg refuses outright, so a schema with one unconverted
                # routine used to abort the whole apply for a reason that had
                # nothing to do with the schema.
                statements = [s for s in all_statements if has_executable_sql(s)]
                skipped = len(all_statements) - len(statements)
                total = len(statements)
                # Applying a large schema is dominated by the round trip to
                # the server -- roughly a thousand statements for five
                # hundred tables, each waiting for an answer. Timing it, and
                # saying so, is the difference between "the tool is slow"
                # and "the server is answering N statements a second."
                started_at = time.monotonic()
                failures = []
                already_there = []
                recoveries = []
                from tgdatabridge.utils.ddl_errors import describe_skip, is_already_exists_error
                for idx, stmt in enumerate(statements, start=1):
                    try:
                        target.execute_ddl(stmt + ";")
                    except Exception as exc:  # noqa: BLE001 - surface which statement failed
                        preview = " ".join(stmt.split())[:200]
                        # "That object is already there" is not a failure:
                        # it is the state the statement was asking for. A
                        # foreign key cannot be written as ADD CONSTRAINT
                        # IF NOT EXISTS in standard SQL, so re-applying a
                        # script -- the normal apply/adjust/apply-again
                        # loop -- used to stop dead on error 1826 with the
                        # schema half-applied. Recorded, named in the
                        # summary and logged, never silent.
                        if is_already_exists_error(exc, stmt):
                            already_there.append(f"{idx}/{total}: {describe_skip(stmt)}")
                            logger.warning(
                                f"{label}: statement {idx}/{total} skipped -- "
                                f"{describe_skip(stmt)} already exists on the target ({exc})")
                            continue
                        # A foreign key the target refuses because the
                        # *rows* break it is not a conversion problem and
                        # not a reason to abandon the run. Every engine can
                        # create the constraint without re-checking rows
                        # that are already there, so that is what happens --
                        # the constraint ends up on the target, enforced
                        # from now on, no data touched, and the orphans are
                        # counted and named in the summary. See
                        # tgdatabridge.core.fk_recovery.
                        explanation = None
                        try:
                            from tgdatabridge.core.fk_violations import (
                                diagnose, is_foreign_key_violation)
                            if is_foreign_key_violation(exc):
                                found = diagnose(target, stmt)
                                if found is not None:
                                    explanation = found.message()
                                from tgdatabridge.core.fk_recovery import POLICY_STOP, recover
                                if fk_policy != POLICY_STOP:
                                    done = recover(target, stmt, fk_policy, found)
                                    if done is not None and done.ok:
                                        recoveries.append(done)
                                        logger.warning(
                                            f"{label}: statement {idx}/{total} -- "
                                            f"{done.one_line()}")
                                        if total <= 50 or idx % 10 == 0 or idx == total:
                                            self._emit_progress(idx, total)
                                        continue
                                    if done is not None and done.error:
                                        # The recovery itself failed. Say so
                                        # alongside the original error rather
                                        # than instead of it -- "NOT VALID was
                                        # also refused" is the useful half.
                                        explanation = (
                                            (explanation + "\n\n") if explanation else "")
                                        explanation += (
                                            "The tool also tried to create this constraint "
                                            "without re-checking the existing rows, and that "
                                            f"was refused too: {done.error}")
                        except Exception:  # noqa: BLE001
                            explanation = explanation or None
                        if not continue_on_error:
                            return _ApplyFailed(idx, total, str(exc), preview,
                                                explanation)
                        failures.append(f"{idx}/{total}: {exc}\n  -> {preview}")
                        logger.warning(f"{label}: statement {idx}/{total} failed: {exc}")
                    # Throttled: one signal per statement on a large schema
                    # means tens of thousands of queued cross-thread events,
                    # each re-entering the modal progress dialog.
                    if total <= 50 or idx % 10 == 0 or idx == total:
                        self._emit_progress(idx, total)
                return (total - len(failures) - len(already_there), skipped,
                        failures, already_there, time.monotonic() - started_at,
                        recoveries)
            finally:
                target.close()

        def on_success(result):
            if isinstance(result, _PreflightBlocked):
                self._offer_preflight_actions(
                    result.problems, ddl_text, label, empty_hint, engine)
                return
            if isinstance(result, _ApplyFailed):
                self._offer_continue_past_error(result, ddl_text, label, empty_hint)
                return
            count, skipped, failures, already_there, elapsed, recoveries = result
            rate = (count / elapsed) if elapsed > 0 else 0
            logger.info(
                f"{label}: applied {count} DDL statement(s) to {engine} target in "
                f"{elapsed:.1f}s ({rate:.0f} statements/second). Most of that is the "
                f"round trip to the server, one statement at a time.")
            note = ""
            if already_there:
                note += ("\n\n" + f"{len(already_there)} object(s) were already on the target "
                         "and were left as they are:\n  "
                         + "\n  ".join(already_there[:8]))
                if len(already_there) > 8:
                    note += f"\n  ... and {len(already_there) - 8} more (see the log)."
            if failures:
                note += ("\n\n" + f"{len(failures)} statement(s) did not apply:\n  "
                         + "\n  ".join(failures[:8]))
                if len(failures) > 8:
                    note += f"\n  ... and {len(failures) - 8} more (see the log)."
            if recoveries:
                from tgdatabridge.core.fk_recovery import summarise
                text = summarise(recoveries)
                note += "\n\n" + text
                for line in text.splitlines():
                    if line.strip():
                        logger.warning(f"{label}: {line}")
            # The step that creates tables does not copy rows, and after it
            # every table on the target is legitimately empty. That reads
            # as a failed migration unless it is said out loud, so it is.
            next_step = ""
            if label == "Apply DDL to Target":
                next_step = (
                    "\n\nThis created the schema only -- no rows have been copied yet, so "
                    "every table on the target is empty at this point. That is expected.\n\n"
                    "Next: \"4a. Dry Run (Plan)\" to check what would be copied, then "
                    "\"4b. Migrate Data\" to copy it.")
                if self.report_view.post_load_view.toPlainText().strip() \
                        != _POST_LOAD_PLACEHOLDER.strip():
                    next_step += (
                        "\n\nAfter the data is in, \"5. Apply Post-Load DDL\" adds the "
                        "constraints, indexes and triggers that \"Defer constraints\" held back.")
            if skipped:
                # `+=`, not `=`: this used to overwrite the failure list
                # above, so a script that both failed a statement and
                # carried a manual-conversion placeholder reported only
                # the placeholder and hid every real failure.
                note += (f"\n\n{skipped} comment-only block(s) were skipped -- these are objects "
                         f"flagged for manual conversion. See the Assessment Report tab.")
                logger.warning(
                    f"{label}: skipped {skipped} comment-only block(s) for objects that still "
                    f"need manual conversion.")
            QMessageBox.information(
                self, "DDL applied",
                f"Successfully applied {count} statements to {engine}.{note}{next_step}")
            self._refresh_target_schema(silent=True)
            if then is not None:
                then()

        logger.info(f"{label}: applying DDL to {engine} target...")
        self._run_async(label, task, on_success)

    def _offer_continue_past_error(self, failure, ddl_text: str, label: str,
                                   empty_hint: str) -> None:
        """A statement was rejected. Stop, or find out what else is wrong?

        Stopping at the first failure is the right default -- statement
        N+1 usually depends on statement N. But on a real schema the
        script is hundreds of statements long, and a class of problem
        that affects thirty tables then surfaces thirty times, one run and
        one dialog each. Offering to carry on and collect every failure
        turns that into a single list.

        Safe to offer because the script is now idempotent: re-running it
        skips what already exists (see tgdatabridge.utils.ddl_errors) rather than
        failing on it, so "continue" does not double-apply the 116
        statements that already worked.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Operation failed")
        box.setText(f"Statement {failure.index}/{failure.total} failed: {failure.error}")
        box.setInformativeText(
            (f"{failure.explanation}\n\n" if failure.explanation else "")
            + f"  -> {failure.preview}...\n\n"
            f"{failure.index - 1} statement(s) applied before this one. Nothing after it "
            f"has run.\n\n"
            "If this looks like one problem repeated across many tables, \"Apply the rest\" "
            "will run the whole script through and list every statement that fails, so you "
            "can see all of it at once. Anything already created is skipped rather than "
            "re-applied.")
        carry_on = box.addButton("Apply the rest and list every failure",
                                 QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Stop", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(carry_on)
        box.exec()
        if failure.explanation:
            for line in failure.explanation.splitlines():
                if line.strip():
                    logger.error(f"  {line.strip()}")
        if box.clickedButton() is carry_on:
            logger.warning(
                f"{label}: continuing past statement {failure.index} to collect every failure.")
            self._apply_ddl_text(ddl_text, label, empty_hint,
                                 preflight=False, continue_on_error=True)
            return
        logger.error(f"{label}: stopped at statement {failure.index}/{failure.total}: "
                     f"{failure.error}")

    def _offer_preflight_actions(self, problems, ddl_text: str, label: str,
                                 empty_hint: str, engine: str) -> None:
        """Blocked by a table already on the target -- now what?

        The check itself is right: `CREATE TABLE IF NOT EXISTS` over a
        table with different columns silently does nothing, and the run
        then fails several statements later on a foreign key, with the
        schema half-applied. But reporting that and stopping leaves the
        user to fix it by hand in another tool, and the two things they
        actually want are both things this one can do.
        """
        names = [p.table_name for p in problems]
        listed = ", ".join(names[:8]) + (f", and {len(names) - 8} more"
                                         if len(names) > 8 else "")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Already on the target")
        box.setText(
            f"{len(names)} table(s) already exist on the target with different "
            f"columns: {listed}")
        box.setInformativeText(
            "Nothing has been applied. \"CREATE TABLE IF NOT EXISTS\" would leave them "
            "as they are, and later statements would fail against them.\n\n"
            "• Leave them out — unticks them in the object tree so the rest of the "
            "schema can be applied. Nothing on the target is touched.\n"
            "• Replace them — drops those tables on the target, with everything in "
            "them, and applies the schema fresh.\n\n"
            "You can also point the target at a different database or schema and "
            "start again.")
        box.setDetailedText("\n\n".join(p.message for p in problems))
        leave_out = box.addButton("Leave them out", QMessageBox.ButtonRole.AcceptRole)
        replace = box.addButton("Replace them…", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(leave_out)
        box.exec()

        if box.clickedButton() is leave_out:
            self._untick_tables(names)
            return
        if box.clickedButton() is replace:
            self._replace_target_tables(names, ddl_text, label, empty_hint, engine)
            return
        logger.info(f"{label}: cancelled -- the target still has {listed}.")

    def _untick_tables(self, names) -> None:
        """Take the conflicting tables out of the selection, so the rest of
        the schema can go across. Uses the same checkboxes the user would
        have used by hand -- and the same dependency handling, so a
        foreign key or view that pointed at one of them is dropped from
        the script too rather than failing on the target."""
        folded = {n.lower() for n in names}
        tree_item = self.schema_tree._category_items.get("Tables")
        unticked = []
        if tree_item is not None:
            for i in range(tree_item.childCount()):
                child = tree_item.child(i)
                if child.text(0).lower() in folded:
                    child.setCheckState(0, Qt.CheckState.Unchecked)
                    unticked.append(child.text(0))
        self._update_selection_label()
        for name in unticked:
            logger.warning(f"Unticked '{name}': it already exists on the target.")
        QMessageBox.information(
            self, "Left out",
            f"{len(unticked)} table(s) were unticked in the object tree.\n\n"
            "Press \"2. Convert Schema\" to rebuild the script without them, then "
            "\"3. Apply DDL to Target\" again.\n\n"
            "Anything that depended on them -- a foreign key into one, a view that "
            "selects from one -- is left out too, and named in the log.")

    def _replace_target_tables(self, names, ddl_text: str, label: str,
                               empty_hint: str, engine: str) -> None:
        """Drop exactly the conflicting tables, then apply the schema.

        Deliberately only those tables, not the whole rollback script:
        "Reset Target…" exists for wiping the loaded schema, and using it
        here would drop objects that were not in the way.
        """
        scope = self.active_schema or self.schema
        target_schema = self.target_params.schema if self.target_params else None
        folded = {n.lower() for n in names}
        tables = [t for t in (scope.tables if scope else []) if t.name.lower() in folded]
        if not tables:
            QMessageBox.warning(
                self, "Nothing to drop",
                "Those tables are not in the loaded schema, so this tool has no "
                "definition to drop them by. Drop or rename them on the target "
                "yourself, or point the target at a different database.")
            return

        statements = [ddl_generator._drop_table_ddl(t, engine, target_schema)
                      for t in tables]
        script = "\n".join(statements)

        where = (f"{engine} — {self.target_params.host}/{self.target_params.database}"
                 if self.target_params else engine)
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Critical)
        confirm.setWindowTitle("Drop these tables?")
        confirm.setText(f"Permanently delete {len(tables)} table(s) from {where}?")
        confirm.setInformativeText(
            ", ".join(t.name for t in tables)
            + "\n\nEverything in them goes with them. This cannot be undone.\n\n"
            "If there is anything in these tables you need, cancel and back them up "
            "first -- or migrate into a different database instead.")
        confirm.setDetailedText(script)
        confirm.setStandardButtons(QMessageBox.StandardButton.Cancel
                                   | QMessageBox.StandardButton.Yes)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        confirm.button(QMessageBox.StandardButton.Yes).setText("Drop and apply")
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            logger.info("Replace on target: cancelled; nothing was dropped.")
            return

        logger.warning(
            f"Dropping {len(tables)} conflicting table(s) on the target: "
            + ", ".join(t.name for t in tables))
        self._apply_ddl_text(
            script, "Drop conflicting tables",
            empty_hint="Nothing to drop.",
            preflight=False, continue_on_error=True,
            then=lambda: self._apply_ddl_text(ddl_text, label, empty_hint))

    def _dry_run_migration(self) -> None:
        if not self.source_params or not self.target_params or not self.schema:
            QMessageBox.warning(self, "Prerequisites missing",
                                 "Connect source and target, and load the schema (step 1), first.")
            return

        engine = self.target_engine_combo.currentText()
        source_engine = self.source_engine_combo.currentText()
        tables = self._migration_tables()

        def task():
            from tgdatabridge.core.migrator import plan_schema
            source = _make_source_connector(source_engine, self.source_params)
            source.connect()
            target = _make_target_connector(engine, self.target_params)
            target.connect()
            try:
                def table_progress(done, total):
                    self._emit_progress(done, total)
                return plan_schema(source, target, tables, progress_cb=table_progress)
            finally:
                source.close()
                target.close()

        def on_success(plan):
            not_ready = plan.not_ready
            logger.info(
                f"Dry run: {plan.total_source_rows} source row(s) across {len(plan.tables)} table(s) would be migrated."
            )
            for t in plan.tables:
                if t.warnings or t.error:
                    for w in t.warnings:
                        logger.warning(f"  {t.table_name}: {w}")
                    if t.error:
                        logger.error(f"  {t.table_name}: {t.error}")
            if not_ready:
                logger.error(f"Not ready: {', '.join(not_ready)}")
                QMessageBox.warning(
                    self, "Dry run: not ready",
                    f"{len(not_ready)} of {len(plan.tables)} table(s) are not ready to migrate "
                    f"(see the log for details): {', '.join(not_ready)}\n\n"
                    f"No data was written -- this was a dry run.",
                )
            else:
                QMessageBox.information(
                    self, "Dry run: ready",
                    f"All {len(plan.tables)} table(s) look ready. Estimated {plan.total_source_rows} row(s) "
                    f"would be migrated.\n\nNo data was written -- this was a dry run.",
                )

        logger.info(f"Dry run: checking {len(tables)} table(s)...")
        self._run_async("Dry Run (Plan)", task, on_success)

    def _migrate_data(self) -> None:
        if not self.source_params or not self.target_params or not self.schema:
            QMessageBox.warning(self, "Prerequisites missing",
                                 "Connect source and target, load and convert the schema, and apply DDL first.")
            return

        engine = self.target_engine_combo.currentText()
        source_engine = self.source_engine_combo.currentText()
        tables = self._migration_tables()

        # Migrate Data is the only irreversible step in the tool -- it
        # writes rows into somebody's database. Every other button either
        # reads, or produces a script to review. It used to start the
        # moment it was clicked, with no statement of what was about to be
        # written or where, which made "I clicked the wrong thing" an
        # expensive mistake. Confirm first.
        if not self._confirm_migration(tables, source_engine, engine):
            logger.info("Migrate Data cancelled before it started.")
            return

        # Every run is checkpointed under an id derived from (source
        # database, target database, schema, target engine) -- see
        # app_storage.checkpoint_id_for. Re-running "Migrate Data" after a
        # partial failure therefore resumes automatically: a table already
        # fully copied last time is skipped outright, and a table that
        # failed partway through picks up from its last completed batch,
        # rather than the whole run starting over from nothing.
        checkpoint_id = app_storage.checkpoint_id_for(
            self.source_params.database, self.target_params.database, self.schema.name, engine)
        checkpoint = app_storage.load_checkpoint(checkpoint_id) or app_storage.MigrationCheckpoint(
            checkpoint_id=checkpoint_id, schema_name=self.schema.name,
            source_engine=source_engine, target_engine=engine,
        )

        max_workers = self.max_workers_spin.value()

        # Row-count *estimate* per table (Table.row_count_estimate, e.g.
        # Oracle's ALL_TABLES.NUM_ROWS -- often stale, sometimes 0/unknown),
        # looked up once here rather than on every progress callback.
        # _MigrationProgressDialog.setObjectProgress falls back to an
        # indeterminate bar when a table's estimate is 0.
        row_estimates = {t.name: (getattr(t, "row_count_estimate", 0) or 0) for t in tables}

        # Lets a person pause between batches without aborting the run --
        # the source/target connections stay open, checkpointing keeps
        # working exactly as it does for an ordinary run, and Resume
        # continues right where it left off. See migrator.migrate_table's
        # own docstring on `pause_event` for why a plain threading.Event
        # is enough even with several parallel workers. Cleared here would
        # mean "start paused", which nothing asked for -- .set() means
        # "run".
        pause_event = threading.Event()
        pause_event.set()
        self._migrate_pause_event = pause_event

        # Per-table elapsed time, for the migration log's Duration/
        # Throughput columns (see build_migration_log_html's `durations`
        # parameter and _write_migration_log below) -- previously always
        # empty, because nothing measured or passed it, which is exactly
        # backwards on a migration slow enough that "which table(s) ate
        # the 8 hours" is the first thing anyone reading the log wants to
        # know. Built from the same per-batch `progress` callback the log
        # line "TABLE: N rows copied..." already came from, so no new
        # instrumentation is needed -- just the first and last time each
        # table name is seen. A table seen only once (small enough to
        # finish in a single batch) has no *interval* to measure and is
        # deliberately left out rather than reported as a fabricated
        # 0-second duration; see _write_migration_log's own comment.
        table_first_seen: dict[str, float] = {}
        table_last_seen: dict[str, float] = {}

        def task():
            from tgdatabridge.core.migrator import migrate_schema
            from tgdatabridge.core.retry import RetryPolicy

            def progress(table_name, count):
                logger.info(f"  {table_name}: {count} rows copied...")
                now = time.monotonic()
                table_first_seen.setdefault(table_name, now)
                table_last_seen[table_name] = now
                self._emit_object_progress(table_name, count, row_estimates.get(table_name, 0))

            def table_progress(done, total):
                self._emit_progress(done, total)

            def on_retry(table_name, attempt, exc, delay):
                logger.warning(
                    f"  {table_name}: transient error on attempt {attempt} ({exc}); "
                    f"retrying in {delay:.1f}s..."
                )

            # Coalesces per-batch progress writes (SCALE.md section 1.5):
            # at COPY speeds across several shards, rewriting the whole
            # checkpoint file after every batch is itself a bottleneck.
            # Terminal transitions still write immediately via .flush.
            checkpoint_writer = app_storage.CheckpointWriter(checkpoint)

            if max_workers > 1:
                # No single pre-connected source/target here: migrate_schema
                # opens up to `max_workers` connections of its own from these
                # factories (see its own docstring on why -- a bare DB-API
                # connection isn't safe to share across worker threads) and
                # closes all of them itself once every table is done.
                def source_factory():
                    conn = _make_source_connector(source_engine, self.source_params)
                    conn.connect()
                    return conn

                def target_factory():
                    conn = _make_target_connector(engine, self.target_params)
                    conn.connect()
                    return conn

                def on_shard_plan(table_name, shard_count, reason):
                    if shard_count > 1:
                        logger.info(f"{table_name}: split into {shard_count} parallel shards ({reason}).")

                return migrate_schema(
                    None, None, tables, progress_cb=progress, table_progress_cb=table_progress,
                    retry_policy=RetryPolicy(), on_retry=on_retry,
                    checkpoint=checkpoint, on_checkpoint_update=checkpoint_writer.request,
                    on_checkpoint_flush=checkpoint_writer.flush,
                    max_workers=max_workers, source_factory=source_factory, target_factory=target_factory,
                    # A big table is otherwise migrated by exactly one worker
                    # while the others idle -- see SCALE.md section 1.2. Only
                    # tables with a single-column integer PK and enough rows
                    # to be worth it are actually split; everything else
                    # falls back to one whole-table unit as before.
                    max_shards_per_table=max_workers,
                    on_shard_plan=on_shard_plan,
                    pause_event=pause_event,
                )

            source = _make_source_connector(source_engine, self.source_params)
            source.connect()
            target = _make_target_connector(engine, self.target_params)
            target.connect()
            try:
                return migrate_schema(
                    source, target, tables, progress_cb=progress, table_progress_cb=table_progress,
                    retry_policy=RetryPolicy(), on_retry=on_retry,
                    checkpoint=checkpoint, on_checkpoint_update=checkpoint_writer.request,
                    on_checkpoint_flush=checkpoint_writer.flush,
                    pause_event=pause_event,
                )
            finally:
                source.close()
                target.close()

        def on_success(report):
            self._migrate_pause_event = None
            failed = report.failed_tables
            skipped = [r.table_name for r in report.results if r.skipped]
            unvalidated = report.unvalidated_tables
            logger.info(f"Data migration complete: {report.total_rows} rows across {len(tables)} table(s).")
            if skipped:
                logger.info(f"Skipped (already completed on a previous run): {', '.join(skipped)}")
            if unvalidated:
                for r in report.results:
                    if r.table_name in unvalidated and r.validation is not None:
                        v = r.validation
                        logger.warning(f"  {r.table_name}: {v.summary}")
                logger.warning(f"Unvalidated tables: {', '.join(unvalidated)}")
            if failed:
                # Log each failing table's actual exception text, not just
                # its name -- migrate_table already captures this in
                # MigrationResult.error, but it was previously discarded
                # here, leaving no way to see *why* a table failed short of
                # re-running the whole migration under a debugger.
                for r in report.results:
                    if not r.succeeded:
                        logger.error(f"  {r.table_name}: {r.error}")
                logger.error(f"Failed tables: {', '.join(failed)}")
                QMessageBox.warning(self, "Migration finished with errors",
                                     f"{report.total_rows} rows migrated. Failed tables: {', '.join(failed)}\n\n"
                                     f"Progress has been checkpointed -- re-running Migrate Data will resume "
                                     f"instead of starting over.\n\nSee the log for the specific error on each table.")
            else:
                # Nothing left that could ever need resuming -- remove the
                # checkpoint rather than leaving it behind indefinitely.
                app_storage.delete_checkpoint(checkpoint_id)
                if unvalidated:
                    # Deliberately not the word "failed": every row was
                    # copied and the migration itself succeeded. What
                    # could not be *confirmed* is that what landed is
                    # identical, which is a different thing and should
                    # not send someone hunting for lost data.
                    QMessageBox.warning(
                        self, "Migration complete, with validation warnings",
                        f"{report.total_rows} rows migrated across {len(tables)} table(s). "
                        f"All of them copied without error, but {len(unvalidated)} could not "
                        f"be fully verified afterwards:\n\n{', '.join(unvalidated)}\n\n"
                        f"The log says which check was inconclusive for each one.")
                else:
                    QMessageBox.information(self, "Migration complete",
                                             f"{report.total_rows} rows migrated across {len(tables)} table(s), "
                                             f"validated against the target.")
            self._migration_table_durations = {
                name: table_last_seen[name] - table_first_seen[name]
                for name in table_first_seen
                if name in table_last_seen and table_last_seen[name] > table_first_seen[name]
            }
            self._write_migration_log(report, tables, source_engine, engine)
            self._refresh_target_schema(silent=True)

        def migrate_metrics(report):
            return {
                "source_engine": source_engine,
                "target_engine": engine,
                "rows": report.total_rows,
                "failed_tables": len(report.failed_tables),
                "table_count": len(tables),
            }

        def on_failed() -> None:
            self._migrate_pause_event = None

        self._migration_started_at = datetime.datetime.now()
        self._migration_log_mark = len(logger.history())
        logger.info(f"Migrating data for {len(tables)} table(s)...")
        self._run_async(
            "Migrate Data", task, on_success, metrics_extra=migrate_metrics, on_failed=on_failed,
            dialog_factory=lambda: _MigrationProgressDialog("Migrate Data…", self, pause_event),
        )

    # ------------------------------------------------ Migrate Data support

    def _confirm_migration(self, tables, source_engine: str, target_engine: str) -> bool:
        """Summarise what is about to be written, and where, before any of
        it happens. Returns True if the user chose to go ahead.

        The target line is deliberately the most prominent thing here:
        the mistake this is guarding against is not "I didn't mean to
        migrate", it is "I didn't realise which database I was pointed
        at".
        """
        names = [t.name for t in tables]
        shown = ", ".join(names[:12])
        if len(names) > 12:
            shown += f", +{len(names) - 12} more"

        estimated = sum(getattr(t, "row_count_estimate", 0) or 0 for t in tables)
        rows_line = (f"about {estimated:,} rows (estimated)" if estimated
                     else "row counts not yet estimated")

        # A partial selection has to be stated here. The tree is on the
        # other side of the window and its checkboxes all start ticked, so
        # "I unticked that an hour ago" is exactly the thing a user
        # forgets before pressing the one irreversible button.
        excluded = self.schema_tree.excluded_summary()
        excluded_line = ("Some objects are unticked and will NOT be migrated:\n  "
                         + "\n  ".join(excluded) + "\n\n") if excluded else ""

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Migrate data?")
        box.setText("This writes data into the target database.")
        box.setInformativeText(
            f"From   {source_engine} — {self.source_params.host or 'local file'}"
            f"/{self.source_params.database}\n"
            f"Into     {target_engine} — {self.target_params.host}/{self.target_params.database}"
            f"  (schema {self.target_params.schema or 'default'})\n\n"
            f"{len(names)} table(s), {rows_line}.\n\n"
            + excluded_line +
            "A detailed HTML log of the run will be saved to this machine when it finishes."
        )
        box.setDetailedText("Tables to migrate:\n" + "\n".join(names)
                            + ("\n\nLeft out of this run:\n" + "\n".join(excluded)
                               if excluded else ""))
        go = box.addButton("Migrate data", QMessageBox.AcceptRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(box.buttons()[-1])   # Cancel is the safe default
        box.exec()
        return box.clickedButton() is go

    def _write_migration_log(self, report, tables, source_engine: str, target_engine: str) -> None:
        """Write the run's detailed HTML log and offer to open it.

        Best-effort throughout: a problem writing the log must never turn
        a completed migration into a reported failure, so every step is
        guarded and the worst case is a log line saying it couldn't be
        written.
        """
        try:
            from tgdatabridge.reports.migration_log import build_migration_log_html

            started = getattr(self, "_migration_started_at", None)
            finished = datetime.datetime.now()
            mark = getattr(self, "_migration_log_mark", 0)

            context = {
                "Schema": self.schema.name if self.schema else "",
                "Source": f"{source_engine} — {self.source_params.host or 'local file'}"
                          f"/{self.source_params.database}",
                "Target": f"{target_engine} — {self.target_params.host}/{self.target_params.database}",
                "Target schema": self.target_params.schema or "default",
                "Tables selected": len(tables),
                "Parallel workers": self.max_workers_spin.value(),
                "Run by": logger.current_actor(),
                "Started": started.strftime("%Y-%m-%d %H:%M:%S") if started else "unknown",
                "Finished": finished.strftime("%Y-%m-%d %H:%M:%S"),
                "Elapsed": (str(finished - started).split(".")[0] if started else "unknown"),
            }

            # Populated (per table, in seconds) right before this call by
            # the migration task above -- see its own comment. Absent
            # entirely for a run this method didn't come from (there is
            # none, today, but getattr keeps this method safe to call on
            # its own), which build_migration_log_html already treats
            # the same as "unknown" for every table.
            durations = getattr(self, "_migration_table_durations", None)

            html_text = build_migration_log_html(
                report, context=context, log_records=logger.history()[mark:], durations=durations)

            directory = logger.log_dir()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"migration-{finished.strftime('%Y-%m-%d_%H%M%S')}.html"
            path.write_text(html_text, encoding="utf-8")
            logger.info(f"Migration log written: {path}")
            self._offer_to_open(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not write the migration log: {exc}")

    def _offer_to_open(self, path) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Migration log saved")
        box.setText("A detailed log of this run has been saved to this machine.")
        box.setInformativeText(str(path))
        open_button = box.addButton("Open log", QMessageBox.ActionRole)
        box.addButton("Close", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is open_button:
            try:
                from PySide6.QtCore import QUrl
                from PySide6.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            except Exception:  # noqa: BLE001
                pass

    def _refresh_target_schema(self, silent: bool = False) -> None:
        if not self.target_params:
            QMessageBox.warning(self, "No target connection", "Connect to the target database first.")
            return

        engine = self.target_engine_combo.currentText()

        def task():
            from tgdatabridge.core.target_introspector import introspect_target
            target = _make_target_connector(engine, self.target_params)
            target.connect()
            try:
                # MySQL has no schema_name property (there's no separate
                # "schema" concept distinct from the database itself), so it
                # falls back to the connection's database name; Oracle,
                # Postgres, SQL Server, and DB2 all expose the resolved
                # schema (defaulting to the connecting username/"public"/
                # "dbo"/the connecting user's ID respectively) via
                # schema_name.
                schema_name = getattr(target, "schema_name", self.target_params.database)
                return introspect_target(target, engine, schema_name)
            finally:
                target.close()

        def on_success(objects):
            self.target_schema_tree.load_objects(objects)
            logger.info(
                f"Target schema: {len(objects.tables)} tables, {len(objects.views)} views, "
                f"{len(objects.sequences)} sequences, {len(objects.routines)} routines/triggers."
            )
            if self.schema is not None:
                from tgdatabridge.core.schema_diff import compute_diff
                diff = compute_diff(self.schema, objects)
                self.report_view.set_diff(diff)
                if diff.fully_in_sync:
                    logger.info("Schema diff: target matches the loaded source schema.")
                else:
                    outstanding = ", ".join(
                        f"{c.label} ({len(c.only_in_source)} only-in-source, {len(c.only_in_target)} only-in-target)"
                        for c in diff.categories() if not c.in_sync
                    )
                    logger.info(f"Schema diff: {outstanding}. See the \"Schema Diff\" tab for details.")
            else:
                self.report_view.clear_diff()

        self._run_async("Refresh Target Schema", task, on_success, silent=silent)

    def _save_report(self) -> None:
        html_text = self.report_view.report_view.toHtml()
        if not html_text.strip():
            QMessageBox.warning(self, "No report", "Convert the schema first (step 2).")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Assessment Report", "migration_assessment_report.html", "HTML Files (*.html)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html_text)
            logger.info(f"Report saved to {path}")

    def _save_ddl(self) -> None:
        ddl_text = self.report_view.ddl_view.toPlainText()
        if not ddl_text.strip():
            QMessageBox.warning(self, "No DDL", "Convert the schema first (step 2).")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Generated DDL", "converted_schema.sql", "SQL Files (*.sql)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(ddl_text)
            logger.info(f"DDL saved to {path}")

    def _save_post_load_ddl(self) -> None:
        """The post-load script, which "Save DDL..." never covered.

        With "Defer constraints" on, every trigger, every index and every
        foreign key lives in this script and nowhere else -- so the one
        file a user most needs to hand to a DBA was the one file the GUI
        could not export.
        """
        text = self.report_view.post_load_view.toPlainText()
        if not text.strip() or text.strip() == _POST_LOAD_PLACEHOLDER.strip():
            QMessageBox.warning(
                self, "Nothing deferred",
                "There is no post-load script. Convert the schema with \"Defer constraints\" "
                "ticked (step 2) to hold constraints, indexes and triggers back until after "
                "the data is migrated.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Post-Load DDL", "post_load.sql", "SQL Files (*.sql)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            logger.info(f"Post-load DDL saved to {path}")

    def _generate_rollback(self) -> None:
        if not self.schema:
            QMessageBox.warning(self, "No schema loaded", "Load and convert the schema first (steps 1-2).")
            return
        target_engine = self.target_engine_combo.currentText()
        target_schema = self.target_params.schema if self.target_params else None
        # The objects the last conversion actually created, not every
        # object that was loaded: dropping a table the user deliberately
        # left out of the migration would be destroying something this
        # tool never put there.
        scope = self.active_schema or self.schema
        rollback_text = ddl_generator.generate_rollback_ddl(scope, target_engine, target_schema)
        self.report_view.set_rollback(rollback_text)
        self.report_view.setCurrentWidget(self.report_view.rollback_view)
        logger.info(f"Rollback script generated for {target_engine} ({len(scope.tables)} table(s)). Review it, then use \"Save Rollback...\" to export.")

    def _apply_rollback(self) -> None:
        """Drop this schema's objects from the target, so "Apply DDL to
        Target" can start from a clean database.

        The tool could generate this script but never run it, which left a
        gap in the one loop a migration is actually tested in: apply, hit a
        problem, reset, try again. Without it, recovering from a
        half-applied schema -- or from a target that already had tables of
        the same names -- meant leaving the tool and running DDL by hand.
        """
        if not self.schema:
            QMessageBox.warning(self, "No schema loaded",
                                "Load and convert a schema first, so the tool knows what to drop.")
            return
        if not self.target_params:
            QMessageBox.warning(self, "No target connection", "Connect to the target database first.")
            return

        engine = self.target_engine_combo.currentText()
        target_schema = self.target_params.schema
        # Same scoping as _generate_rollback: only what was converted.
        scope = self.active_schema or self.schema
        rollback_text = ddl_generator.generate_rollback_ddl(scope, engine, target_schema)

        where = f"{engine} — {self.target_params.host}/{self.target_params.database}"
        counts = (f"{len(scope.tables)} table(s), {len(scope.views)} view(s), "
                  f"{len(scope.routines)} routine(s)/trigger(s)")
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Reset target?")
        confirm.setText(f"Drop this schema's objects from {where}?")
        confirm.setInformativeText(
            f"This permanently deletes {counts} — and any data in them — from the target.\n\n"
            "It only touches objects in the schema currently loaded; anything else in that "
            "database is left alone.\n\nThis cannot be undone."
        )
        confirm.setDetailedText(rollback_text)
        confirm.setStandardButtons(QMessageBox.StandardButton.Cancel |
                                   QMessageBox.StandardButton.Yes)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        confirm.button(QMessageBox.StandardButton.Yes).setText("Drop them")
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            logger.info("Reset target: cancelled.")
            return

        self.report_view.set_rollback(rollback_text)
        logger.warning(f"Reset target: dropping this schema's objects from {where}...")
        # preflight off (the conflicting tables are what we're removing) and
        # continue-on-error (an object that isn't there is not a failure).
        self._apply_ddl_text(
            rollback_text, "Reset Target",
            empty_hint="Convert the schema first (step 2).",
            preflight=False, continue_on_error=True,
        )

    def _save_rollback(self) -> None:
        rollback_text = self.report_view.rollback_view.toPlainText()
        if not rollback_text.strip():
            QMessageBox.warning(self, "No rollback script", "Click \"Rollback Script\" first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Rollback Script", "rollback_schema.sql", "SQL Files (*.sql)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(rollback_text)
            logger.info(f"Rollback script saved to {path}")
