"""AI Review dialog -- the interactive home for three of the four
AI-assisted features (see tgdatabridge/ai/ for what each does): schema
mapping review, plain-English migration requests, and post-migration data
quality review. The fourth (error diagnosis) lives in crash_dialog.py
instead, next to the error it explains, rather than here.

Every action in this dialog is a deliberate, on-demand button click -- it
never fires automatically and never changes what a migration will do by
itself (see tgdatabridge/ai/nl_config.py's own docstring on why that
module only ever proposes a selection for review). Network calls are made
synchronously on the GUI thread: each is a single short request the user
just asked for by clicking a button in this dialog, not a long-running
background operation like Migrate Data, so a plain wait cursor is enough
and this dialog does not need main_window.py's QThread-based
_run_async machinery.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from tgdatabridge.ai.ai_client import AiClient, AiError
from tgdatabridge.core.schema_model import Schema
from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.utils import ai_settings

MAX_QUALITY_SAMPLE_ROWS = 25


class AiReviewDialog(QDialog):
    def __init__(self, schema: Schema, target_engine: str,
                 target_params: Optional[ConnectionParams], parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI Review")
        self.setMinimumSize(640, 480)

        self._schema = schema
        self._target_engine = target_engine
        self._target_params = target_params
        self._ai_settings = ai_settings.load_ai_settings()

        tabs = QTabWidget()
        tabs.addTab(self._build_mapping_tab(), "Schema Mapping Review")
        tabs.addTab(self._build_nl_tab(), "Plain-English Request")
        tabs.addTab(self._build_quality_tab(), "Data Quality Review")

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)

    def _client(self) -> AiClient:
        config = ai_settings.to_ai_config(self._ai_settings)
        return AiClient(config)

    # ------------------------------------------------------ mapping tab

    def _build_mapping_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        intro = QLabel(
            "Ask the AI for a second opinion on this table's already-converted "
            "column types -- it does not change anything, only flags what looks "
            "risky. Table names and column types are sent to your configured "
            "AI provider; no row data.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        row = QHBoxLayout()
        self.mapping_table_combo = QComboBox()
        self.mapping_table_combo.addItems([t.name for t in self._schema.tables] if self._schema else [])
        row.addWidget(self.mapping_table_combo, stretch=1)
        review_btn = QPushButton("Review with AI")
        review_btn.clicked.connect(self._review_mapping)
        row.addWidget(review_btn)
        layout.addLayout(row)

        self.mapping_result = QTextEdit()
        self.mapping_result.setReadOnly(True)
        layout.addWidget(self.mapping_result)
        return widget

    def _review_mapping(self) -> None:
        if not self._require_feature(self._ai_settings.feature_schema_mapping, self.mapping_result):
            return
        table_name = self.mapping_table_combo.currentText()
        table = next((t for t in (self._schema.tables if self._schema else []) if t.name == table_name), None)
        if table is None:
            self.mapping_result.setPlainText("Select a table first.")
            return

        self.mapping_result.setPlainText("Reviewing…")
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            from tgdatabridge.ai.schema_mapper import review_table_mapping
            suggestions = review_table_mapping(self._client(), table, self._target_engine)
        except AiError as exc:
            self.mapping_result.setPlainText(f"⚠ {exc}")
            return
        finally:
            self.unsetCursor()

        if not suggestions:
            self.mapping_result.setPlainText("No columns flagged -- nothing looked risky.")
            return
        lines = []
        for s in suggestions:
            if s.risk == "none" and not s.note:
                continue
            suggestion = f" (suggested: {s.suggested_target_type})" if s.suggested_target_type else ""
            lines.append(f"[{s.risk.upper()}] {s.column}: {s.note}{suggestion}")
        self.mapping_result.setPlainText(
            "\n".join(lines) if lines else "No columns flagged -- nothing looked risky.")

    # ------------------------------------------------------------ nl tab

    def _build_nl_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        intro = QLabel(
            "Describe what you want to migrate in plain English. This only "
            "proposes a table selection for you to review -- it does not select "
            "anything in the schema tree or start a migration by itself. Table "
            "names from the loaded source schema are sent to your configured AI "
            "provider, along with what you type here.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.nl_input = QPlainTextEdit()
        self.nl_input.setPlaceholderText(
            "e.g. \"migrate all the customer and order tables, skip anything archived\"")
        self.nl_input.setMaximumHeight(80)
        layout.addWidget(self.nl_input)

        parse_btn = QPushButton("Parse with AI")
        parse_btn.clicked.connect(self._parse_nl_request)
        layout.addWidget(parse_btn)

        self.nl_result = QTextEdit()
        self.nl_result.setReadOnly(True)
        layout.addWidget(self.nl_result)
        return widget

    def _parse_nl_request(self) -> None:
        if not self._require_feature(self._ai_settings.feature_nl_config, self.nl_result):
            return
        text = self.nl_input.toPlainText()
        table_names = [t.name for t in self._schema.tables] if self._schema else []

        self.nl_result.setPlainText("Working…")
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            from tgdatabridge.ai.nl_config import parse_migration_request
            result = parse_migration_request(self._client(), text, table_names)
        except AiError as exc:
            self.nl_result.setPlainText(f"⚠ {exc}")
            return
        finally:
            self.unsetCursor()

        lines = [f"Suggested tables ({len(result.tables)}):"]
        lines.extend(f"  - {t}" for t in result.tables) or lines.append("  (none)")
        if result.filters:
            lines.append("")
            lines.append("Suggested filters:")
            lines.extend(f"  - {t}: {f}" for t, f in result.filters.items())
        if result.notes:
            lines.append("")
            lines.append(f"Notes: {result.notes}")
        lines.append("")
        lines.append(
            "Nothing has been selected automatically -- use the schema tree's own "
            "checkboxes to select these tables before converting/migrating.")
        self.nl_result.setPlainText("\n".join(lines))

    # ------------------------------------------------------- quality tab

    def _build_quality_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        intro = QLabel(
            "Reviews a small sample of already-migrated rows on the target for "
            "likely data problems. Unlike the other two tabs, this sends real "
            "migrated row values (up to "
            f"{MAX_QUALITY_SAMPLE_ROWS} rows) to your configured AI provider -- "
            "leave \"Post-migration data quality review\" off in AI Settings if "
            "that isn't acceptable for this data.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        row = QHBoxLayout()
        self.quality_table_combo = QComboBox()
        self.quality_table_combo.addItems([t.name for t in self._schema.tables] if self._schema else [])
        row.addWidget(self.quality_table_combo, stretch=1)
        review_btn = QPushButton("Review Sample")
        review_btn.clicked.connect(self._review_quality)
        row.addWidget(review_btn)
        layout.addLayout(row)

        self.quality_result = QTextEdit()
        self.quality_result.setReadOnly(True)
        layout.addWidget(self.quality_result)
        return widget

    def _review_quality(self) -> None:
        if not self._require_feature(self._ai_settings.feature_data_quality, self.quality_result):
            return
        if self._target_params is None:
            self.quality_result.setPlainText("Connect and migrate to a target first.")
            return
        table_name = self.quality_table_combo.currentText()
        table = next((t for t in (self._schema.tables if self._schema else []) if t.name == table_name), None)
        if table is None:
            self.quality_result.setPlainText("Select a table first.")
            return

        self.quality_result.setPlainText("Sampling target data…")
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            columns, sample_rows, target_count = self._sample_target_table(table)
        except Exception as exc:  # noqa: BLE001 -- any driver error, surfaced plainly
            self.unsetCursor()
            self.quality_result.setPlainText(f"⚠ Could not read a sample from the target: {exc}")
            return

        if not sample_rows:
            self.unsetCursor()
            self.quality_result.setPlainText("No rows found on the target for this table.")
            return

        try:
            from tgdatabridge.ai.data_quality import review_sample
            findings = review_sample(
                self._client(), table_name, columns, sample_rows,
                source_row_count=table.row_count_estimate, target_row_count=target_count)
        except AiError as exc:
            self.quality_result.setPlainText(f"⚠ {exc}")
            return
        finally:
            self.unsetCursor()

        if not findings:
            self.quality_result.setPlainText("Nothing looked wrong in this sample.")
            return
        lines = [f"[{f.severity.upper()}] {f.column or '(table)'}: {f.description}" for f in findings]
        self.quality_result.setPlainText("\n".join(lines))

    def _sample_target_table(self, table) -> tuple:
        """Best-effort, generic sample read from the target -- plain
        `SELECT * ... / SELECT COUNT(*) ...` works across every SQL target
        engine this tool supports (Oracle, MySQL, PostgreSQL, SQL Server,
        Db2); driver-specific errors are caught by the caller and shown
        plainly rather than crashing the dialog. MongoDB's connector
        implements the same DBConnector.execute() surface via its own
        translation layer, so this needs no engine-specific branching."""
        from tgdatabridge.core.connector_factory import make_target_connector
        qualified = f"{table.schema}.{table.name}" if table.schema else table.name
        connector = make_target_connector(self._target_engine, self._target_params)
        connector.connect()
        try:
            columns = [c.name for c in table.columns]
            rows: List[tuple] = []
            for row in connector.execute(f"SELECT * FROM {qualified}"):
                rows.append(tuple(row))
                if len(rows) >= MAX_QUALITY_SAMPLE_ROWS:
                    break
            count_row = next(iter(connector.execute(f"SELECT COUNT(*) FROM {qualified}")), None)
            target_count = int(count_row[0]) if count_row else len(rows)
            return columns, rows, target_count
        finally:
            connector.close()

    # ----------------------------------------------------------- helpers

    def _require_feature(self, flag: bool, result_widget) -> bool:
        if not self._ai_settings.enabled:
            result_widget.setPlainText("AI features are off. Turn them on in AI Settings first.")
            return False
        if not flag:
            result_widget.setPlainText("This feature is switched off in AI Settings.")
            return False
        return True
