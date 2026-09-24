"""A read-only dialog listing past "Convert Schema" runs
(tgdatabridge.utils.app_storage.load_conversion_history), with buttons to reopen
an auto-saved copy of a past run's report/DDL in the OS's default viewer."""
from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout,
)

from tgdatabridge.utils import app_storage


class HistoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Conversion History")
        self.setMinimumSize(640, 420)

        self.records = app_storage.load_conversion_history()

        self.list_widget = QListWidget()
        for record in self.records:
            item = QListWidgetItem(record.display_label)
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            self.list_widget.addItem(item)
        self.list_widget.currentRowChanged.connect(self._on_selection_changed)

        self.detail_label = QLabel("Select a run to see details.")
        self.detail_label.setWordWrap(True)

        self.open_report_btn = QPushButton("Open Report")
        self.open_report_btn.clicked.connect(self._open_report)
        self.open_report_btn.setEnabled(False)
        self.open_ddl_btn = QPushButton("Open DDL")
        self.open_ddl_btn.clicked.connect(self._open_ddl)
        self.open_ddl_btn.setEnabled(False)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.open_report_btn)
        buttons_row.addWidget(self.open_ddl_btn)

        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        if not self.records:
            layout.addWidget(QLabel(
                'No conversion runs recorded yet — run "2. Convert Schema" at least once and it will show up here.'))
        layout.addWidget(self.list_widget)
        layout.addWidget(self.detail_label)
        layout.addLayout(buttons_row)
        layout.addWidget(close_buttons)

    def _current_record(self):
        row = self.list_widget.currentRow()
        if 0 <= row < len(self.records):
            return self.records[row]
        return None

    def _on_selection_changed(self, _row: int) -> None:
        record = self._current_record()
        if record is None:
            self.detail_label.setText("Select a run to see details.")
            self.open_report_btn.setEnabled(False)
            self.open_ddl_btn.setEnabled(False)
            return
        self.detail_label.setText(
            f"{record.total_objects} object(s) — {record.automatic_pct}% automatic, "
            f"~{record.estimated_manual_hours}h estimated manual effort, "
            f"{record.action_item_count} action item(s). "
            f"Target schema: {record.target_schema or '(default)'}"
        )
        self.open_report_btn.setEnabled(bool(record.report_path))
        self.open_ddl_btn.setEnabled(bool(record.ddl_path))

    def _open_report(self) -> None:
        record = self._current_record()
        if record and record.report_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(record.report_path))

    def _open_ddl(self) -> None:
        record = self._current_record()
        if record and record.ddl_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(record.ddl_path))
