"""Compact, read-only tree showing what currently exists in the *target*
schema right now -- populated after "Apply DDL to Target" / "Migrate Data"
(or on demand via the toolbar's "Refresh Target Schema" button) so the
target's actual state is visible without switching to a separate DB
client. Deliberately smaller and simpler than the source SchemaTree: no
checkboxes, no per-column detail, capped height -- it's a quick "did this
land?" glance, not a working tree."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

from tgdatabridge.core.target_introspector import TargetObjects


class TargetSchemaTree(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Target schema (as migrated)"])
        self.setColumnCount(1)
        self.setAlternatingRowColors(True)
        self.setMaximumHeight(180)  # short pane, by request -- a glance, not a workspace
        self.show_placeholder(
            "Connect a target and run \"Apply DDL to Target\" / \"Migrate Data\", "
            "or click \"Refresh Target Schema\", to see it here."
        )

    def load_objects(self, objects: TargetObjects) -> None:
        self.clear()
        categories = [
            ("Tables", objects.tables),
            ("Views", objects.views),
            ("Sequences", objects.sequences),
            ("Routines / Triggers", objects.routines),
        ]
        counts = getattr(objects, "row_counts", None) or {}
        for label, names in categories:
            heading = f"{label} ({len(names)})"
            if label == "Tables" and counts:
                # "499 tables, all empty" is the single most useful thing
                # this pane can say right after Apply DDL -- it is exactly
                # what makes people think the migration failed when in
                # fact the data step has not been run yet.
                with_rows = sum(1 for n in names if counts.get(n, 0) > 0)
                total_rows = sum(max(0, counts.get(n, 0)) for n in names)
                if with_rows == 0:
                    heading += " - all empty, no data migrated yet"
                else:
                    heading += (f" - {with_rows} with data, "
                                f"~{total_rows:,} row(s) in total")
            cat_item = QTreeWidgetItem(self, [heading])
            for name in names:
                text = name
                if label == "Tables" and name in counts:
                    rows = max(0, counts[name])
                    text = f"{name}  —  {'empty' if rows == 0 else f'~{rows:,} rows'}"
                QTreeWidgetItem(cat_item, [text])
        self.expandAll()

    def show_placeholder(self, message: str) -> None:
        self.clear()
        placeholder = QTreeWidgetItem(self, [message])
        placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
