"""Left-hand tree view listing the introspected schema, with checkboxes
so the user can pick which objects to convert / migrate."""
from __future__ import annotations

from typing import Dict, List, Optional, Set

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QMenu, QTreeWidget, QTreeWidgetItem

from tgdatabridge.core.schema_model import ConversionStatus, Schema

_STATUS_ICON = {
    ConversionStatus.AUTOMATIC: "✅",
    ConversionStatus.AUTOMATIC_WITH_WARNINGS: "⚠️",
    ConversionStatus.MANUAL: "❌",
    ConversionStatus.NOT_SUPPORTED: "\U0001f6ab",
}

CATEGORIES = ("Tables", "Views", "Sequences", "Routines / Triggers")

# Above this many total objects, skip auto-expanding every category. Qt has
# to lay out every visible row the moment a category is expanded, and doing
# that for tens of thousands of rows at once (a large Oracle banking core
# easily has 20k+ objects once tables/views/sequences/routines are all
# counted) is what makes "Load Schema" feel like it's hung. Leaving
# categories collapsed keeps the initial render fast; the user can still
# expand one category at a time to inspect it.
_AUTO_EXPAND_THRESHOLD = 500


class SchemaTree(QTreeWidget):
    """Every object carries a checkbox; unticking one leaves it out of the
    generated DDL and out of the data migration.

    Two things this has to get right beyond drawing the boxes:

    * **The selection survives a rebuild.** The tree is reloaded after
      "Convert Schema" to show each object's conversion status, and that
      rebuild used to re-tick everything -- so a user who unticked three
      tables, converted, then clicked "Migrate Data" silently got all of
      them back. Tick state is now remembered by (category, name) across
      a reload of the same schema.
    * **The category row means something.** It is tri-state, so it shows
      at a glance whether a category is fully, partly or not selected,
      and its label carries the count ("Tables (3 of 5 selected)").
    """

    selection_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Object", "Status"])
        self.setColumnWidth(0, 260)
        self.setAlternatingRowColors(True)
        self._category_items: Dict[str, QTreeWidgetItem] = {}
        self._counts: Dict[str, int] = {}
        self._loading = False
        self.itemChanged.connect(self._on_item_changed)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.setToolTip(
            "Untick any object to leave it out of the generated DDL and out of "
            "the data migration. Right-click for select all / none / invert.")

    # ------------------------------------------------------------ loading

    def load_schema(self, schema: Schema, converted: bool = False,
                    preserve_selection: bool = True) -> None:
        """Rebuild the tree from `schema`.

        `preserve_selection` keeps whatever the user had unticked -- right
        for the reload that follows a conversion, wrong for a fresh
        "Load Schema", which is a new object graph and starts fully
        selected.
        """
        remembered = self.unchecked_names() if preserve_selection else {}

        # Bulk-inserting thousands of items with the widget's normal update/
        # repaint cycle active is far slower than doing it with updates
        # suspended and re-enabling them once the whole tree is built.
        self._loading = True
        self.setUpdatesEnabled(False)
        try:
            self.clear()
            self._category_items = {}
            self._counts = {}

            categories = [
                ("Tables", schema.tables),
                ("Views", schema.views),
                ("Sequences", schema.sequences),
                ("Routines / Triggers", schema.routines),
            ]
            total_objects = sum(len(objects) for _label, objects in categories)

            for label, objects in categories:
                cat_item = QTreeWidgetItem(self, ["", ""])
                cat_item.setFlags(cat_item.flags() | Qt.ItemFlag.ItemIsAutoTristate
                                  | Qt.ItemFlag.ItemIsUserCheckable)
                cat_item.setCheckState(0, Qt.CheckState.Checked)
                self._category_items[label] = cat_item
                self._counts[label] = len(objects)

                skip = remembered.get(label, set())
                for obj in objects:
                    status_text = ""
                    if converted:
                        icon = _STATUS_ICON.get(obj.status, "")
                        status_text = f"{icon} {obj.status.value}"
                    child = QTreeWidgetItem(cat_item, [obj.name, status_text])
                    child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    child.setCheckState(
                        0, Qt.CheckState.Unchecked if obj.name in skip
                        else Qt.CheckState.Checked)
                    child.setData(0, Qt.ItemDataRole.UserRole, obj)

            if total_objects <= _AUTO_EXPAND_THRESHOLD:
                self.expandAll()
            else:
                for cat_item in self._category_items.values():
                    cat_item.setExpanded(False)
        finally:
            self.setUpdatesEnabled(True)
            self._loading = False
        self._refresh_labels()

    # ---------------------------------------------------------- selection

    def checked_objects(self, category: str) -> list:
        cat_item = self._category_items.get(category)
        if cat_item is None:
            return []
        result = []
        for i in range(cat_item.childCount()):
            child = cat_item.child(i)
            if child.checkState(0) == Qt.CheckState.Checked:
                result.append(child.data(0, Qt.ItemDataRole.UserRole))
        return result

    def unchecked_names(self) -> Dict[str, Set[str]]:
        """{category: {name, ...}} for everything currently unticked."""
        out: Dict[str, Set[str]] = {}
        for label, cat_item in self._category_items.items():
            names = {cat_item.child(i).text(0)
                     for i in range(cat_item.childCount())
                     if cat_item.child(i).checkState(0) != Qt.CheckState.Checked}
            if names:
                out[label] = names
        return out

    def excluded_summary(self) -> List[str]:
        """One line per category that has something unticked, for the log
        and the confirmation dialogs -- so a partial run always says so."""
        lines = []
        for label in CATEGORIES:
            cat_item = self._category_items.get(label)
            if cat_item is None:
                continue
            excluded = [cat_item.child(i).text(0)
                        for i in range(cat_item.childCount())
                        if cat_item.child(i).checkState(0) != Qt.CheckState.Checked]
            if not excluded:
                continue
            shown = ", ".join(excluded[:6])
            if len(excluded) > 6:
                shown += f", and {len(excluded) - 6} more"
            lines.append(f"{label}: {len(excluded)} left out ({shown})")
        return lines

    def has_any_checked(self) -> bool:
        return any(self.checked_objects(label) for label in CATEGORIES)

    def set_all_checked(self, checked: bool, category: Optional[str] = None) -> None:
        labels = [category] if category else list(CATEGORIES)
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._loading = True
        try:
            for label in labels:
                cat_item = self._category_items.get(label)
                if cat_item is None:
                    continue
                for i in range(cat_item.childCount()):
                    cat_item.child(i).setCheckState(0, state)
                cat_item.setCheckState(0, state)
        finally:
            self._loading = False
        self._refresh_labels()
        self.selection_changed.emit()

    def invert_selection(self, category: Optional[str] = None) -> None:
        labels = [category] if category else list(CATEGORIES)
        self._loading = True
        try:
            for label in labels:
                cat_item = self._category_items.get(label)
                if cat_item is None:
                    continue
                for i in range(cat_item.childCount()):
                    child = cat_item.child(i)
                    child.setCheckState(
                        0, Qt.CheckState.Unchecked
                        if child.checkState(0) == Qt.CheckState.Checked
                        else Qt.CheckState.Checked)
        finally:
            self._loading = False
        self._refresh_labels()
        self.selection_changed.emit()

    # ------------------------------------------------------------ internals

    def _on_item_changed(self, _item, _column) -> None:
        if self._loading:
            return
        self._refresh_labels()
        self.selection_changed.emit()

    def _refresh_labels(self) -> None:
        """Category rows read 'Tables (5)' when everything is selected and
        'Tables (3 of 5 selected)' when it isn't, so a partial selection is
        visible without expanding the category."""
        was_loading = self._loading
        self._loading = True
        try:
            for label, cat_item in self._category_items.items():
                total = self._counts.get(label, cat_item.childCount())
                checked = sum(
                    1 for i in range(cat_item.childCount())
                    if cat_item.child(i).checkState(0) == Qt.CheckState.Checked)
                text = (f"{label} ({total})" if checked == total
                        else f"{label} ({checked} of {total} selected)")
                if cat_item.text(0) != text:
                    cat_item.setText(0, text)
        finally:
            self._loading = was_loading

    def _show_menu(self, pos) -> None:
        if not self._category_items:
            return
        item = self.itemAt(pos)
        category = None
        if item is not None:
            top = item if item.parent() is None else item.parent()
            for label, cat_item in self._category_items.items():
                if cat_item is top:
                    category = label
                    break

        menu = QMenu(self)
        if category:
            menu.addAction(f"Select all in {category}",
                           lambda: self.set_all_checked(True, category))
            menu.addAction(f"Clear all in {category}",
                           lambda: self.set_all_checked(False, category))
            menu.addAction(f"Invert {category}",
                           lambda: self.invert_selection(category))
            menu.addSeparator()
        menu.addAction("Select all objects", lambda: self.set_all_checked(True))
        menu.addAction("Clear all objects", lambda: self.set_all_checked(False))
        menu.addAction("Invert selection", lambda: self.invert_selection())
        menu.exec(self.viewport().mapToGlobal(pos))
