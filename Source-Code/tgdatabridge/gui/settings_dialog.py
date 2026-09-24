"""Settings dialog -- the optional shared storage folder for a team
deployment (ENTERPRISE_READINESS.md section 5, item 4). Follows the same
style as connection_dialog.py/history_dialog.py: a plain QDialog with a
QFormLayout and a standard OK/Cancel button box.

Central log shipping used to live here too: a checkbox, an endpoint URL
and a minimum level, POSTing every log line to a Splunk/ELK collector.
It has been removed. Logs are written to the local machine only -- see
tgdatabridge.utils.logger.log_dir, which deliberately resolves to the
per-machine directory even when a shared storage folder is configured.
Old settings.json files carrying the removed keys still load; the keys
are simply ignored (see tgdatabridge.utils.settings.load_settings)."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from tgdatabridge.utils import settings as app_settings


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)

        self._settings = app_settings.load_settings()

        # -- shared storage --------------------------------------------------
        shared_intro = QLabel(
            "Point this machine at a shared folder (a mapped network drive "
            "or UNC path) so a team shares connection profiles, conversion "
            "history, migration checkpoints, logs, and metrics instead of "
            "each keeping their own local copy. Leave blank to keep using "
            "this machine's own local storage. There's no locking or "
            "conflict resolution -- this suits a team where one person runs "
            "a migration at a time, sharing history/checkpoints across "
            "whichever machine they're on, not true concurrent multi-user "
            "access. Takes effect the next time the app starts."
        )
        shared_intro.setWordWrap(True)

        self.shared_path_edit = QLineEdit(self._settings.shared_storage_path)
        self.shared_path_edit.setPlaceholderText(r"\\fileserver\share\TGDataBridge (optional)")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_shared_path)

        shared_path_row = QWidget()
        shared_path_layout = QHBoxLayout(shared_path_row)
        shared_path_layout.setContentsMargins(0, 0, 0, 0)
        shared_path_layout.addWidget(self.shared_path_edit)
        shared_path_layout.addWidget(browse_btn)

        shared_form = QFormLayout()
        shared_form.addRow("Shared storage folder", shared_path_row)

        logs_note = QLabel(
            "Logs are always written to this machine, in "
            "%APPDATA%\\TGDataBridge\\logs, even when a shared folder is set "
            "above — a record of what went wrong belongs on the machine it "
            "happened on. Each Migrate Data run also writes a detailed HTML "
            "log there."
        )
        logs_note.setWordWrap(True)
        logs_note.setStyleSheet("color: #666;")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(shared_intro)
        layout.addLayout(shared_form)
        layout.addWidget(logs_note)
        layout.addWidget(buttons)

    def _browse_shared_path(self) -> None:
        start_dir = self.shared_path_edit.text().strip() or ""
        chosen = QFileDialog.getExistingDirectory(self, "Choose a shared storage folder", start_dir)
        if chosen:
            self.shared_path_edit.setText(chosen)

    def _on_accept(self) -> None:
        self._settings = app_settings.AppSettings(
            shared_storage_path=self.shared_path_edit.text().strip(),
        )
        app_settings.save_settings(self._settings)
        self.accept()

    def result_settings(self) -> app_settings.AppSettings:
        """The settings as saved when the dialog was accepted -- callers
        use this instead of re-loading from disk so there's no risk of a
        race with save_settings() having not flushed yet."""
        return self._settings
