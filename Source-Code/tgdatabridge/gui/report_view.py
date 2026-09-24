"""Right-hand panel: tabs for generated DDL, the HTML assessment report, and
a source-vs-target schema diff.

Uses QTextBrowser (built into Qt Widgets) rather than QWebEngineView so the
tool has no extra native-rendering dependency to install.
"""
from __future__ import annotations

import html as _html

from PySide6.QtWidgets import QPlainTextEdit, QTabWidget, QTextBrowser, QWidget

from tgdatabridge.core.schema_diff import SchemaDiff

_DIFF_PLACEHOLDER_HTML = (
    "<p style='font-family: sans-serif; color: #666;'>"
    "Load a source schema and click \"Refresh Target Schema\" to compare "
    "what's on the target against what's loaded from Oracle.</p>"
)

_POST_LOAD_PLACEHOLDER = (
    "Empty because \"Defer constraints\" is off, so the Generated DDL tab already contains "
    "everything.\n\n"
    "Turn it on before converting to hold primary keys, unique/check constraints, indexes and "
    "triggers back until after the data has been migrated. Loading into a table with no indexes "
    "is substantially faster, building each index once over the finished table produces a denser "
    "index, and triggers never fire on migrated rows.\n\n"
    "The trade-off: a constraint violation then surfaces at the end of the run rather than on the "
    "first bad row, so run \"4a. Dry Run (Plan)\" first on anything large."
)


class ReportView(QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.ddl_view = QPlainTextEdit()
        self.ddl_view.setReadOnly(True)
        self.ddl_view.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")

        self.report_view = QTextBrowser()
        self.report_view.setOpenExternalLinks(True)

        self.diff_view = QTextBrowser()
        self.diff_view.setHtml(_DIFF_PLACEHOLDER_HTML)

        self.rollback_view = QPlainTextEdit()
        self.rollback_view.setReadOnly(True)
        self.rollback_view.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        self.rollback_view.setPlainText(
            "Click \"Rollback Script\" (after converting a schema) to generate a DROP script that "
            "undoes the Generated DDL tab's CREATE statements, in reverse dependency order."
        )

        # Populated only when "Defer constraints" is on -- the constraints,
        # indexes and triggers held back until after the data loads (see
        # SCALE.md section 1.3). Left empty otherwise, since with deferral
        # off there is no second script: everything is in Generated DDL.
        self.post_load_view = QPlainTextEdit()
        self.post_load_view.setReadOnly(True)
        self.post_load_view.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        self.post_load_view.setPlainText(_POST_LOAD_PLACEHOLDER)

        self.addTab(self.ddl_view, "Generated DDL")
        self.addTab(self.report_view, "Assessment Report")
        self.addTab(self.diff_view, "Schema Diff")
        self.addTab(self.rollback_view, "Rollback Script")
        self.addTab(self.post_load_view, "Post-Load DDL")

    def set_ddl(self, ddl_text: str) -> None:
        self.ddl_view.setPlainText(ddl_text)

    def set_post_load_ddl(self, ddl_text: str) -> None:
        self.post_load_view.setPlainText(ddl_text or _POST_LOAD_PLACEHOLDER)

    def set_report(self, html_text: str) -> None:
        self.report_view.setHtml(html_text)

    def set_rollback(self, rollback_text: str) -> None:
        self.rollback_view.setPlainText(rollback_text)

    def set_diff(self, diff: SchemaDiff) -> None:
        self.diff_view.setHtml(_render_diff_html(diff))

    def clear_diff(self) -> None:
        self.diff_view.setHtml(_DIFF_PLACEHOLDER_HTML)


def _render_diff_html(diff: SchemaDiff) -> str:
    parts = [
        "<html><body style='font-family: sans-serif; font-size: 13px;'>",
        "<h2>Source vs. Target Schema Diff</h2>",
        "<p style='color: #666;'>Comparing the loaded Oracle source schema against what's "
        "currently on the target (as of the last \"Refresh Target Schema\"). Names are "
        "compared case-insensitively; a package's individual members and, for a Db2 target, "
        "a multi-event trigger's per-event copies are matched against their expected "
        "flattened/split names rather than the source object's own name.</p>",
    ]
    if diff.fully_in_sync:
        parts.append(
            "<p style='color: #1a7f37; font-weight: bold;'>"
            "Everything loaded from the source matches what's on the target — nothing outstanding.</p>"
        )

    for category in diff.categories():
        parts.append(f"<h3>{_html.escape(category.label)}</h3>")
        if category.in_sync:
            parts.append("<p style='color: #1a7f37;'>In sync.</p>")
            continue
        if category.only_in_source:
            parts.append(
                "<p><b style='color: #9a6700;'>Only in source</b> "
                "(not yet on the target):</p><ul>"
                + "".join(f"<li>{_html.escape(n)}</li>" for n in category.only_in_source)
                + "</ul>"
            )
        if category.only_in_target:
            parts.append(
                "<p><b style='color: #cf222e;'>Only on target</b> "
                "(not in the loaded source):</p><ul>"
                + "".join(f"<li>{_html.escape(n)}</li>" for n in category.only_in_target)
                + "</ul>"
            )
        if category.in_both:
            parts.append(
                f"<p style='color: #666;'>{len(category.in_both)} present in both.</p>"
            )

    parts.append("</body></html>")
    return "".join(parts)
